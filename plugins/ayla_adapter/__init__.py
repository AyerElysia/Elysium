"""Ayla independent-application adapter.

注意：不要在包初始化时导入 plugin（plugin.py 会通过 ``from .config``
触发本模块，若本模块再 ``from .plugin`` 会形成循环导入，导致
``cannot import name 'AylaAdapter'``）。
"""

from .config import AylaAdapterConfig
from .sender import AylaSender

__all__ = [
    "AylaAdapterConfig",
    "AylaSender",
]
