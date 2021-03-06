

from .warmup import WarmupLRScheduler

class NoDecay(WarmupLRScheduler):
    
    def get_lr_warmpup(self, num_iter) -> float:
        return self.start_lr * num_iter / self.warmup_iter
    
    def get_lr_decay(self, num_iter) -> float:
        return self.start_lr


        
   