# -*- coding: utf-8 -*-
"""X 接口层：读 / 写 / 媒体 / 登录。"""

__all__ = ['XClient', 'XClientError', 'UserPostMonitor', 'Webhook']


def __getattr__(name):
    # Keep low-level imports independent of utility modules that use them.
    if name in ('UserPostMonitor', 'Webhook'):
        from x_apis.user_monitor import UserPostMonitor, Webhook
        return {'UserPostMonitor': UserPostMonitor, 'Webhook': Webhook}[name]
    if name == 'XClient':
        from x_apis.client import XClient
        return XClient
    if name == 'XClientError':
        from x_apis.client_errors import XClientError
        return XClientError
    raise AttributeError(name)
