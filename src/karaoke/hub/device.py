import torch

from karaoke import config


def get_device():
    """Torch device for demucs and the lyrics aligner. DEMUCS_DEVICE overrides auto-detection
    (e.g. "cpu" on a GPU box). ROCm builds of torch expose AMD GPUs through the cuda API."""
    if config.DEMUCS_DEVICE:
        return config.DEMUCS_DEVICE
    if torch.cuda.is_available():
        return 'cuda'
    if torch.backends.mps.is_available():
        return 'mps'
    return 'cpu'
