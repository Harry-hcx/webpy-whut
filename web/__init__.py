#!/usr/bin/env python3
"""web.py: makes web apps (http://webpy.org)

【功能层】web.py 框架的顶层包入口，负责将所有子模块的公开 API 统一导出到 `web` 命名空间。
【设计层】使用 `from module import *` 的平铺导出策略，让使用方可以直接用 `web.application`、
         `web.ctx` 等，无需关心内部模块划分，是典型的"门面模式"（Facade Pattern）。
【上下文层】用户代码 `import web` 后即可使用框架的全部能力，所有功能的入口都在这里汇聚。
"""

# ruff: noqa: F401,F403
# 告知 ruff linter 忽略"未使用导入"和"星号导入"警告，因为这里的导入是为了构建公共 API

# ── 导入各功能子模块（模块对象本身也暴露出去，供需要命名空间访问的场景使用）──
from . import (
    db,           # 数据库抽象层（SQLite / MySQL / PostgreSQL 统一接口）
    debugerror,   # 调试模式下的详细错误页面渲染
    form,         # HTML 表单定义与验证
    http,         # HTTP 工具函数（缓存控制、URL 构建等）
    httpserver,   # 内置 HTTP 开发服务器（基于 wsgiref）
    net,          # 网络工具（HTML 转义、HTTP 日期解析等）
    session,      # 会话管理（磁盘/数据库/内存存储后端）
    template,     # 内置模板引擎（类 Python 语法的 .html 模板）
    utils,        # 通用工具集（Storage、Memoize、ThreadedDict 等）
    webapi,       # Web API 核心（ctx 上下文、HTTPError 体系、输入解析）
    wsgi,         # WSGI 服务器启动与适配
)

# ── 将各子模块的 __all__ 里的符号全部展开到当前命名空间 ──
# 这样 `web.application`、`web.ctx`、`web.input` 等均可直接使用
from .application import *   # application 类、loadhook、unloadhook、autodelegate 等
from .db import *            # database、SQLQuery、SQLParam 等数据库工具
from .debugerror import *    # debugerror 调试错误处理器
from .http import *          # expires、url、changequery 等 HTTP 工具
from .httpserver import *    # runsimple 等服务器启动函数
from .net import *           # websafe、httpdate 等网络工具
from .utils import *         # Storage、memoize、ThreadedDict 等通用工具
from .webapi import *        # config、ctx、input、header、HTTPError 及全部 HTTP 状态码类
from .wsgi import *          # runwsgi 等 WSGI 适配函数

# 框架元数据
__version__ = "0.76"
__author__ = [
    "Aaron Swartz <me@aaronsw.com>",       # 原始作者，互联网著名活动家
    "Anand Chitipothu <anandology@gmail.com>",  # 主要维护者
]
__license__ = "public domain"             # 公有领域，无版权限制
__contributors__ = "see http://webpy.org/changes"