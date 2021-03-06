import torch
from ..global_var import config
from . import lamb_cuda as G
from . import adam_cuda as G_adam
from .. import nccl

class LambOptimizer(torch.optim.Optimizer):
    """
    Lamb optimizer
    """
    def __init__(self, params, lr=1e-3, betas=(0.9, 0.999), eps=1e-8, weight_decay=0, scale=65536, hold_steps=0):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        if not 0.0 <= weight_decay:
            raise ValueError("Invalid weight_decay value: {}".format(weight_decay))

        defaults = dict(lr=lr, betas=betas, eps=eps, weight_decay=weight_decay)
        super().__init__(params, defaults)

        self.load_stream = torch.cuda.Stream()
        self._scale = scale
        self._steps_since_last_scale = 0
        self._hold_steps = hold_steps
    
    @property
    def scale(self):
        return self._scale
    
    @property
    def steps_since_last_scale(self):
        return self._steps_since_last_scale

    @torch.no_grad()
    def justify_scale(self, scale):
        delta = scale / self._scale
        self._scale = scale
        for group in self.param_groups:
            for p in group['params']:
                state = self.state[p]
                if len(state) > 0:
                    state['exp_avg'] *= delta
                    state['exp_avg_sq'] *= delta
        self._steps_since_last_scale = 0

    @torch.no_grad()
    def step(self, closure=None):
        """Performs a single optimization step.
        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        The remaining arguments are deprecated, and are only retained (for the moment) for error-checking purposes.
        """
        
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        # check overflow
        has_inf_or_nan = torch.zeros(1, dtype=torch.uint8, device="cuda")[0]
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is not None:
                    G_adam.f_has_inf_nan(p.grad, has_inf_or_nan)
        
        if "comm" in config:
            nccl.allReduce(has_inf_or_nan.storage(), has_inf_or_nan.storage(), "max", config["comm"])

        if has_inf_or_nan > 0:
            raise OverflowError("Gradient overflow")
        
        self._steps_since_last_scale += 1

        # update parameters
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is not None:
                    if p.grad.is_sparse:
                        raise RuntimeError('Lamb does not support sparse gradients, please consider SparseLamb instead')

                    state = self.state[p]
                    # Lazy state initialization
                    if len(state) == 0:
                        state['step'] = 0
                        # Exponential moving average of gradient values
                        state['exp_avg'] = torch.zeros(p.size(), dtype=torch.half, device=p.device) # on device
                        # Exponential moving average of squared gradient values
                        state['exp_avg_sq'] = torch.zeros(p.size(), dtype=torch.float32, device=p.device)   # on device

                        state['param_fp32'] = torch.empty(p.size(), dtype=torch.float32, device=p.device)   # on device
                        state['param_fp32'].copy_(p)

                    # update the steps for each param group update
                    state['step'] += 1
                    
                    numer = torch.empty(1, dtype=torch.float32, device=p.device)
                    denom = torch.empty(1, dtype=torch.float32, device=p.device)
                    _lr = 0.0 if state["step"] <= self._hold_steps else group['lr']
                    G.f_lamb_prepare(
                        state["param_fp32"],    # fp32
                        p,                      # fp16
                        p.grad,                 # fp16
                        state['exp_avg'],       # fp16: m
                        state["exp_avg_sq"],    # fp32: v
                        group['betas'][0], group['betas'][1],
                        group['eps'],
                        _lr,
                        self._scale,
                        group['weight_decay'],
                        state['step'],
                        numer, denom
                    )

                    nccl.allReduce(numer.storage(), numer.storage(), "sum", config["comm"])
                    nccl.allReduce(denom.storage(), denom.storage(), "sum", config["comm"])
                    _lr = _lr * (numer[0]/(denom[0]+1e-10))**0.5

                    G_adam.f_adam(
                        state["param_fp32"],    # fp32
                        p,                      # fp16
                        p.grad,                 # fp16
                        state['exp_avg'],       # fp16: m
                        state["exp_avg_sq"],    # fp32: v
                        group['betas'][0], group['betas'][1],
                        group['eps'],
                        _lr,
                        self._scale,
                        group['weight_decay'],
                        state['step']
                    )
        
        return loss
    
    def loss_scale(self, loss : torch.Tensor) -> torch.Tensor:
        """
        Backward with loss scale.
        """
        return loss * (self.scale / config['world_size'])
