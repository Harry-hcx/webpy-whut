"""
Web application（Web 应用核心）
(from web.py)

【功能层】实现 WSGI 应用对象，负责 URL 路由分发、请求处理管道（processor 链）、
         自动热重载、子应用挂载、子域名路由等核心功能。
【设计层】以"处理器链"（processor chain）模式组织中间件，每个 processor 是一个
         接受 handler 函数并返回响应的高阶函数，形成递归调用链（类似洋葱模型）。
         使用闭包和高阶函数大量替代继承，体现函数式风格。
【上下文层】用户代码的入口：`app = web.application(urls, globals())`，
         所有请求最终都经由此模块的 wsgifunc 处理。
"""

import itertools
import os
import sys
import traceback
import wsgiref.handlers
from importlib import reload
from inspect import isclass
from io import BytesIO
from urllib.parse import unquote, urlencode, urlparse

from . import browser, httpserver, utils, wsgi
from . import webapi as web
from .debugerror import debugerror
from .py3helpers import iteritems
from .utils import lstrips

__all__ = [
    "application",
    "auto_application",
    "subdir_application",
    "subdomain_application",
    "loadhook",
    "unloadhook",
    "autodelegate",
]


class application:
    """
    【功能层】web.py 的核心应用类，将 URL 模式列表（mapping）映射到处理器类，
             并提供完整的 WSGI 接口、请求测试、热重载、处理器管道等能力。
    【设计层】不继承任何基类（old-style 命名风格保持历史兼容）；
             通过 add_processor 构建处理器链，每个 processor 包裹 handler 形成
             类似"洋葱"的调用结构——这是 web.py 的中间件机制，比显式继承更灵活。
    【上下文层】用户代码 `app = application(urls, globals())` 后，
             可调用 app.run() 启动服务器，或将 app.wsgifunc() 挂载到任意 WSGI 容器。

    Application to delegate requests based on path.

        >>> urls = ("/hello", "hello")
        >>> app = application(urls, globals())
        >>> class hello:
        ...     def GET(self): return "hello"
        >>>
        >>> app.request("/hello").data
        'hello'
    """

    # PY3DOCTEST: b'hello'

    def __init__(self, mapping=(), fvars={}, autoreload=None):
        # 【功能层】初始化应用：设置路由表、变量作用域、处理器链、热重载
        if autoreload is None:
            autoreload = web.config.get("debug", False)  # debug 模式默认开启热重载
        self.init_mapping(mapping)   # 将平铺元组路由表转为 [(pattern, handler), ...] 列表
        self.fvars = fvars           # 保存调用方的全局变量字典，用于按字符串名查找处理器类
        self.processors = []         # 处理器链（中间件列表），按注册顺序执行

        # 【设计层】loadhook/unloadhook 将普通函数包装成符合处理器协议的函数，
        #          _load/_unload 维护 app_stack（子应用栈），实现应用嵌套
        self.add_processor(loadhook(self._load))
        self.add_processor(unloadhook(self._unload))

        if autoreload:

            def main_module_name():
                mod = sys.modules["__main__"]
                file = getattr(
                    mod, "__file__", None
                )  # make sure this works even from python interpreter
                return file and os.path.splitext(os.path.basename(file))[0]

            def modname(fvars):
                """find name of the module name from fvars."""
                file, name = fvars.get("__file__"), fvars.get("__name__")
                if file is None or name is None:
                    return None

                if name == "__main__":
                    # Since the __main__ module can't be reloaded, the module has
                    # to be imported using its file name.
                    name = main_module_name()
                return name

            mapping_name = utils.dictfind(fvars, mapping)
            module_name = modname(fvars)

            def reload_mapping():
                """loadhook to reload mapping and fvars."""
                mod = __import__(module_name, None, None, [""])
                mapping = getattr(mod, mapping_name, None)
                if mapping:
                    self.fvars = mod.__dict__
                    self.init_mapping(mapping)

            self.add_processor(loadhook(Reloader()))
            if mapping_name and module_name:
                # when app is ran as part of a package, this puts the app into
                # `sys.modules` correctly, otherwise the first change to the
                # app module will not be picked up by Reloader
                reload_mapping()

                self.add_processor(loadhook(reload_mapping))

            # load __main__ module usings its filename, so that it can be reloaded.
            if main_module_name() and "__main__" in sys.argv:
                try:
                    __import__(main_module_name())
                except ImportError:
                    pass

    def _load(self):
        web.ctx.app_stack.append(self)

    def _unload(self):
        web.ctx.app_stack = web.ctx.app_stack[:-1]

        if web.ctx.app_stack:
            # this is a sub-application, revert ctx to earlier state.
            oldctx = web.ctx.get("_oldctx")
            if oldctx:
                web.ctx.home = oldctx.home
                web.ctx.homepath = oldctx.homepath
                web.ctx.path = oldctx.path
                web.ctx.fullpath = oldctx.fullpath

    def _cleanup(self):
        # Threads can be recycled by WSGI servers.
        # Clearing up all thread-local state to avoid interefereing with subsequent requests.
        utils.ThreadedDict.clear_all()

    def init_mapping(self, mapping):
        self.mapping = list(utils.group(mapping, 2))

    def add_mapping(self, pattern, classname):
        self.mapping.append((pattern, classname))

    def add_processor(self, processor):
        """
        Adds a processor to the application.

            >>> urls = ("/(.*)", "echo")
            >>> app = application(urls, globals())
            >>> class echo:
            ...     def GET(self, name): return name
            ...
            >>>
            >>> def hello(handler): return "hello, " +  handler()
            ...
            >>> app.add_processor(hello)
            >>> app.request("/web.py").data
            'hello, web.py'
        """
        # PY3DOCTEST: b'hello, web.py'
        self.processors.append(processor)

    def request(
        self,
        localpart="/",
        method="GET",
        data=None,
        host="0.0.0.0:8080",
        headers=None,
        https=False,
        **kw,
    ):
        """Makes request to this application for the specified path and method.
        Response will be a storage object with data, status and headers.

            >>> urls = ("/hello", "hello")
            >>> app = application(urls, globals())
            >>> class hello:
            ...     def GET(self):
            ...         web.header('Content-Type', 'text/plain')
            ...         return "hello"
            ...
            >>> response = app.request("/hello")
            >>> response.data
            'hello'
            >>> response.status
            '200 OK'
            >>> response.headers['Content-Type']
            'text/plain'

        To use https, use https=True.

            >>> urls = ("/redirect", "redirect")
            >>> app = application(urls, globals())
            >>> class redirect:
            ...     def GET(self): raise web.seeother("/foo")
            ...
            >>> response = app.request("/redirect")
            >>> response.headers['Location']
            'http://0.0.0.0:8080/foo'
            >>> response = app.request("/redirect", https=True)
            >>> response.headers['Location']
            'https://0.0.0.0:8080/foo'

        The headers argument specifies HTTP headers as a mapping object
        such as a dict.

            >>> urls = ('/ua', 'uaprinter')
            >>> class uaprinter:
            ...     def GET(self):
            ...         return 'your user-agent is ' + web.ctx.env['HTTP_USER_AGENT']
            ...
            >>> app = application(urls, globals())
            >>> app.request('/ua', headers = {
            ...      'User-Agent': 'a small jumping bean/1.0 (compatible)'
            ... }).data
            'your user-agent is a small jumping bean/1.0 (compatible)'

        """
        # PY3DOCTEST: b'hello'
        # PY3DOCTEST: b'your user-agent is a small jumping bean/1.0 (compatible)'
        _p = urlparse(localpart)
        path = _p.path
        maybe_query = _p.query

        query = maybe_query or ""

        if "env" in kw:
            env = kw["env"]
        else:
            env = {}
        env = dict(
            env,
            HTTP_HOST=host,
            REQUEST_METHOD=method,
            PATH_INFO=path,
            QUERY_STRING=query,
            HTTPS=str(https),
        )
        headers = headers or {}

        for k, v in headers.items():
            env["HTTP_" + k.upper().replace("-", "_")] = v

        if "HTTP_CONTENT_LENGTH" in env:
            env["CONTENT_LENGTH"] = env.pop("HTTP_CONTENT_LENGTH")

        if "HTTP_CONTENT_TYPE" in env:
            env["CONTENT_TYPE"] = env.pop("HTTP_CONTENT_TYPE")

        if method not in ["HEAD", "GET"]:
            data = data or ""

            if isinstance(data, dict):
                q = urlencode(data)
            else:
                q = data

            env["wsgi.input"] = BytesIO(q.encode("utf-8"))
            # if not env.get('CONTENT_TYPE', '').lower().startswith('multipart/') and 'CONTENT_LENGTH' not in env:
            if "CONTENT_LENGTH" not in env:
                env["CONTENT_LENGTH"] = len(q)
        response = web.storage()

        def start_response(status, headers):
            response.status = status
            response.headers = dict(headers)
            response.header_items = headers

        data = self.wsgifunc()(env, start_response)
        response.data = b"".join(data)
        return response

    def browser(self):
        return browser.AppBrowser(self)

    def handle(self):
        fn, args = self._match(self.mapping, web.ctx.path)
        return self._delegate(fn, self.fvars, args)

    def handle_with_processors(self):
        # 【功能层】将请求依次通过所有处理器（中间件），最终调用 handle() 分发路由
        def process(processors):
            try:
                if processors:
                    # 【设计层】递归地将处理器列表"折叠"为嵌套调用：
                    #          p1(lambda: p2(lambda: p3(lambda: handle())))
                    #          外层 processor 先执行，形成"洋葱"调用顺序
                    p, processors = processors[0], processors[1:]
                    return p(lambda: process(processors))
                else:
                    return self.handle()   # 链尾：执行真正的路由分发
            except web.HTTPError:
                raise   # HTTP 异常直接向上传播，由 wsgifunc 捕获转换为响应
            except (KeyboardInterrupt, SystemExit):
                raise   # 系统级信号不吞掉
            except:
                print(traceback.format_exc(), file=web.debug)
                raise self.internalerror()   # 其他异常转为 500

        # 【设计层】处理器按注册顺序排列，但调用时需要"从后往前"包裹，
        #          因此传入完整列表由递归实现正确的包裹顺序
        return process(self.processors)

    def wsgifunc(self, *middleware):
        """
        【功能层】将 application 转换为标准 WSGI 可调用对象，
                 可选包裹额外的 WSGI 中间件。
        【设计层】内部定义 wsgi(env, start_resp) 闭包，捕获 self；
                 build_result 生成器统一处理 str/bytes 响应体；
                 cleanup() 生成器挂在 itertools.chain 末尾，
                 保证响应体全部发送后再清理线程状态（惰性求值保证顺序）。
        【上下文层】app.run() 和 app.cgirun() 都调用此方法获取 WSGI callable。
        """

        def peep(iterator):
            """
            【功能层】"窥探"迭代器：提前消费第一个元素，触发 handle() 的实际执行，
                     确保响应头（status/headers）在 start_response 调用前已被设置。
            【设计层】WSGI 规范要求 start_response 在第一次 yield 数据前调用；
                     使用 itertools.chain 将首元素与剩余迭代器重新拼接，
                     调用方无感知地得到完整响应。
            """
            # wsgi requires the headers first
            # so we need to do an iteration
            # and save the result for later
            try:
                firstchunk = next(iterator)
            except StopIteration:
                firstchunk = ""

            return itertools.chain([firstchunk], iterator)

        def wsgi(env, start_resp):
            # 【功能层】标准 WSGI 入口：清理线程状态 -> 初始化 ctx -> 处理请求 -> 返回响应体迭代器
            self._cleanup()    # 清除上一请求可能残留的线程本地数据

            self.load(env)     # 用 WSGI environ 初始化 web.ctx
            try:
                # 【设计层】只接受全大写 HTTP 方法，防止大小写混用导致的安全或路由问题
                if web.ctx.method.upper() != web.ctx.method:
                    raise web.nomethod()

                result = self.handle_with_processors()
                # 【设计层】检查结果是否为生成器（__next__ 方法），
                #          生成器响应需要先 peep 触发首次迭代以设置响应头
                if result and hasattr(result, "__next__"):
                    result = peep(result)
                else:
                    result = [result]
            except web.HTTPError as e:
                result = [e.data]   # HTTP 异常的 data 就是响应体

            def build_result(result):
                # 【功能层】统一将响应体转为 bytes，WSGI 要求响应体必须是 bytes
                for r in result:
                    if isinstance(r, bytes):
                        yield r
                    else:
                        yield str(r).encode("utf-8")

            result = build_result(result)

            status, headers = web.ctx.status, web.ctx.headers
            start_resp(status, headers)   # 调用 WSGI start_response 发送状态行和响应头

            def cleanup():
                # 【设计层】cleanup 是生成器，挂在响应链末尾，确保响应体全部发送后执行清理
                self._cleanup()
                yield b""  # 必须 yield 才是生成器，但实际不产生有效数据

            return itertools.chain(result, cleanup())

        for m in middleware:
            wsgi = m(wsgi)

        return wsgi

    def run(self, *middleware):
        """
        Starts handling requests. If called in a CGI or FastCGI context, it will follow
        that protocol. If called from the command line, it will start an HTTP
        server on the port named in the first command line argument, or, if there
        is no argument, on port 8080.

        `middleware` is a list of WSGI middleware which is applied to the resulting WSGI
        function.
        """
        return wsgi.runwsgi(self.wsgifunc(*middleware))

    def stop(self):
        """Stops the http server started by run."""
        if httpserver.server:
            httpserver.server.stop()
            httpserver.server = None

    def cgirun(self, *middleware):
        """
        Return a CGI handler. This is mostly useful with Google App Engine.
        There you can just do:

            main = app.cgirun()
        """
        wsgiapp = self.wsgifunc(*middleware)

        try:
            from google.appengine.ext.webapp.util import run_wsgi_app

            return run_wsgi_app(wsgiapp)
        except ImportError:
            # we're not running from within Google App Engine
            return wsgiref.handlers.CGIHandler().run(wsgiapp)

    def gaerun(self, *middleware):
        """
        Starts the program in a way that will work with Google app engine,
        no matter which version you are using (2.5 / 2.7)

        If it is 2.5, just normally start it with app.gaerun()

        If it is 2.7, make sure to change the app.yaml handler to point to the
        global variable that contains the result of app.gaerun()

        For example:

        in app.yaml (where code.py is where the main code is located)

            handlers:
            - url: /.*
              script: code.app

        Make sure that the app variable is globally accessible
        """
        wsgiapp = self.wsgifunc(*middleware)
        try:
            # check what version of python is running
            version = sys.version_info[:2]
            major = version[0]
            minor = version[1]

            if major != 2:
                raise OSError("Google App Engine only supports python 2.5 and 2.7")

            # if 2.7, return a function that can be run by gae
            if minor == 7:
                return wsgiapp
            # if 2.5, use run_wsgi_app
            elif minor == 5:
                from google.appengine.ext.webapp.util import run_wsgi_app

                return run_wsgi_app(wsgiapp)
            else:
                raise OSError("Not a supported platform, use python 2.5 or 2.7")
        except ImportError:
            return wsgiref.handlers.CGIHandler().run(wsgiapp)

    def load(self, env):
        """
        【功能层】用 WSGI environ 字典初始化 web.ctx，建立当前请求的完整上下文。
        【设计层】将 WSGI 的扁平化 environ 解析为语义化的 ctx 属性
                （path、method、ip、protocol 等），屏蔽底层 WSGI 细节。
                处理 lighttpd/nginx 的 PATH_INFO 编码差异（这两个服务器不自动 unquote）。
        【上下文层】每次请求开始时由 wsgifunc 调用，ctx 的所有属性在此处被初始化，
                后续所有处理器和视图函数都依赖这里设置的值。
        """
        ctx = web.ctx
        ctx.clear()
        ctx.status = "200 OK"
        ctx.headers = []
        ctx.output = ""
        ctx.environ = ctx.env = env
        ctx.host = env.get("HTTP_HOST")

        if env.get("wsgi.url_scheme") in ["http", "https"]:
            ctx.protocol = env["wsgi.url_scheme"]
        elif env.get("HTTPS", "").lower() in ["on", "true", "1"]:
            ctx.protocol = "https"
        else:
            ctx.protocol = "http"
        ctx.homedomain = ctx.protocol + "://" + env.get("HTTP_HOST", "[unknown]")
        ctx.homepath = os.environ.get("REAL_SCRIPT_NAME", env.get("SCRIPT_NAME", ""))
        ctx.home = ctx.homedomain + ctx.homepath
        # @@ home is changed when the request is handled to a sub-application.
        # @@ but the real home is required for doing absolute redirects.
        ctx.realhome = ctx.home
        ctx.ip = env.get("REMOTE_ADDR")
        ctx.method = env.get("REQUEST_METHOD")
        try:
            ctx.path = bytes(env.get("PATH_INFO"), "latin1").decode("utf8")
        except UnicodeDecodeError:  # If there are Unicode characters...
            ctx.path = env.get("PATH_INFO")

        # http://trac.lighttpd.net/trac/ticket/406 requires:
        if env.get("SERVER_SOFTWARE", "").startswith(("lighttpd/", "nginx/")):
            ctx.path = lstrips(env.get("REQUEST_URI").split("?")[0], ctx.homepath)
            # Apache and CherryPy webservers unquote urls but lighttpd and nginx do not.
            # Unquote explicitly for lighttpd and nginx to make ctx.path uniform across
            # all servers.
            ctx.path = unquote(ctx.path)

        if env.get("QUERY_STRING"):
            ctx.query = "?" + env.get("QUERY_STRING", "")
        else:
            ctx.query = ""

        ctx.fullpath = ctx.path + ctx.query

        for k, v in iteritems(ctx):
            # convert all string values to unicode values and replace
            # malformed data with a suitable replacement marker.
            if isinstance(v, bytes):
                ctx[k] = v.decode("utf-8", "replace")

        # status must always be str
        ctx.status = "200 OK"

        ctx.app_stack = []

    def _delegate(self, f, fvars, args=[]):
        """
        【功能层】将路由匹配结果（处理器 f）实例化并调用对应 HTTP 方法，返回响应。
        【设计层】支持多种处理器形式：
                 - None → 404
                 - application 实例 → 子应用递归处理
                 - 类（isclass）→ 实例化后调用 GET/POST 等方法
                 - "redirect /url" 字符串 → 直接重定向
                 - "module.ClassName" 字符串 → 动态导入并实例化
                 - 可调用对象 → 直接调用
                 这种多态分发替代了大量 if/isinstance 判断，体现鸭子类型思想。
        """
        def handle_class(cls):
            # 【功能层】实例化处理器类并调用对应 HTTP 方法（GET/POST/HEAD 等）
            meth = web.ctx.method
            if meth == "HEAD" and not hasattr(cls, meth):
                meth = "GET"   # HEAD 无专用方法时降级为 GET
            if not hasattr(cls, meth):
                raise web.nomethod(cls)
            tocall = getattr(cls(), meth)
            return tocall(*args)

        if f is None:
            raise web.notfound()
        elif isinstance(f, application):
            return f.handle_with_processors()
        elif isclass(f):
            return handle_class(f)
        elif isinstance(f, str):
            if f.startswith("redirect "):
                url = f.split(" ", 1)[1]
                if web.ctx.method == "GET":
                    x = web.ctx.env.get("QUERY_STRING", "")
                    if x:
                        url += "?" + x
                raise web.redirect(url)
            elif "." in f:
                mod, cls = f.rsplit(".", 1)
                mod = __import__(mod, None, None, [""])
                cls = getattr(mod, cls)
            else:
                cls = fvars[f]
            return handle_class(cls)
        elif hasattr(f, "__call__"):
            return f()
        else:
            return web.notfound()

    def _match(self, mapping, value):
        """
        【功能层】遍历路由表，对 value（URL 路径或 host）进行正则匹配，
                 返回 (处理器, 捕获分组列表) 或 (None, None)。
        【设计层】路由模式被包裹为 `^pattern\Z`（\Z 匹配字符串末尾，比 $ 更严格），
                 利用 re_subm 同时做替换和匹配，支持 "redirect /new" 形式的
                 字符串处理器中使用反向引用（如 r"/foo/(.*)" → r"/bar/\1"）。
        """
        for pat, what in mapping:
            if isinstance(what, application):
                if value.startswith(pat):
                    f = lambda: self._delegate_sub_application(pat, what)
                    return f, None
                else:
                    continue
            elif isinstance(what, str):
                what, result = utils.re_subm(rf"^{pat}\Z", what, value)
            else:
                result = utils.re_compile(rf"^{pat}\Z").match(value)

            if result:  # it's a match
                return what, [x for x in result.groups()]
        return None, None

    def _delegate_sub_application(self, dir, app):
        """Deletes request to sub application `app` rooted at the directory `dir`.
        The home, homepath, path and fullpath values in web.ctx are updated to mimic request
        to the subapp and are restored after it is handled.

        @@Any issues with when used with yield?
        """
        web.ctx._oldctx = web.storage(web.ctx)
        web.ctx.home += dir
        web.ctx.homepath += dir
        web.ctx.path = web.ctx.path[len(dir) :]
        web.ctx.fullpath = web.ctx.fullpath[len(dir) :]
        return app.handle_with_processors()

    def get_parent_app(self):
        if self in web.ctx.app_stack:
            index = web.ctx.app_stack.index(self)
            if index > 0:
                return web.ctx.app_stack[index - 1]

    def notfound(self):
        """Returns HTTPError with '404 not found' message"""
        parent = self.get_parent_app()
        if parent:
            return parent.notfound()
        else:
            return web._NotFound()

    def internalerror(self):
        """Returns HTTPError with '500 internal error' message"""
        parent = self.get_parent_app()
        if parent:
            return parent.internalerror()
        elif web.config.get("debug"):
            return debugerror()
        else:
            return web._InternalError()


def with_metaclass(mcls):
    """
    【功能层】辅助函数：用指定元类 mcls 重新创建一个类，兼容 Python 2/3 的元类语法差异。
    【设计层】Python 3 的元类语法是 `class Foo(Base, metaclass=Meta)`，
             此函数提供一种通过装饰器指定元类的等价方式，保持代码整洁。
             body.pop("__dict__") 是必要的清理步骤，否则复制的类字典中的
             __dict__ 描述符会干扰新类的创建。
    【上下文层】被 auto_application 用于创建带元类的 page 基类。
    """
    def decorator(cls):
        body = vars(cls).copy()
        # clean out class body
        body.pop("__dict__", None)
        body.pop("__weakref__", None)
        return mcls(cls.__name__, cls.__bases__, body)

    return decorator


class auto_application(application):
    """
    【功能层】自动路由应用：继承 application，通过元类自动将子类注册为路由，
             无需手动维护 URLs 元组。
    【设计层】核心是内部类 metapage（继承 type）：每当用户定义 `class foo(app.page)`
             时，metapage.__init__ 被调用，自动以类名（如 "/foo"）或 path 属性
             注册路由——这是元类（metaclass）最典型的应用场景：
             "在类定义时自动执行注册逻辑"。
             with_metaclass 装饰器解决了 Python 2/3 元类语法兼容问题。
    【上下文层】适合快速原型开发，用类定义代替显式路由配置，减少样板代码。

    Application similar to `application` but urls are constructed
    automatically using metaclass.

        >>> app = auto_application()
        >>> class hello(app.page):
        ...     def GET(self): return "hello, world"
        ...
        >>> class foo(app.page):
        ...     path = '/foo/.*'
        ...     def GET(self): return "foo"
        >>> app.request("/hello").data
        'hello, world'
        >>> app.request('/foo/bar').data
        'foo'
    """

    # PY3DOCTEST: b'hello, world'
    # PY3DOCTEST: b'foo'

    def __init__(self):
        application.__init__(self)

        class metapage(type):
            def __init__(klass, name, bases, attrs):
                type.__init__(klass, name, bases, attrs)
                path = attrs.get("path", "/" + name)

                # path can be specified as None to ignore that class
                # typically required to create a abstract base class.
                if path is not None:
                    self.add_mapping(path, klass)

        @with_metaclass(metapage)  # little hack needed for Py2 and Py3 compatibility
        class page:
            path = None

        self.page = page


# The application class already has the required functionality of subdir_application
subdir_application = application


class subdomain_application(application):
    r"""
    Application to delegate requests based on the host.

        >>> urls = ("/hello", "hello")
        >>> app = application(urls, globals())
        >>> class hello:
        ...     def GET(self): return "hello"
        >>>
        >>> mapping = (r"hello\.example\.com", app)
        >>> app2 = subdomain_application(mapping)
        >>> app2.request("/hello", host="hello.example.com").data
        'hello'
        >>> response = app2.request("/hello", host="something.example.com")
        >>> response.status
        '404 Not Found'
        >>> response.data
        'not found'
    """

    # PY3DOCTEST: b'hello'
    # PY3DOCTEST: b'not found'

    def handle(self):
        host = web.ctx.host.split(":")[0]  # strip port
        fn, args = self._match(self.mapping, host)
        return self._delegate(fn, self.fvars, args)

    def _match(self, mapping, value):
        for pat, what in mapping:
            if isinstance(what, str):
                what, result = utils.re_subm("^" + pat + "$", what, value)
            else:
                result = utils.re_compile("^" + pat + "$").match(value)

            if result:  # it's a match
                return what, [x for x in result.groups()]
        return None, None


def loadhook(h):
    """
    【功能层】将一个"请求前"钩子函数 h 包装成符合处理器协议的函数。
    【设计层】返回的 processor 接受 handler（下一步处理器）作为参数，
             先调用 h()，再调用 handler()，实现"前置钩子"语义。
             这是高阶函数（higher-order function）的典型应用。
    【上下文层】application.__init__ 用此函数包装 _load（将 app 压栈）和
             reload_mapping（热重载），以及 Reloader()（模块文件变更检测）。

    Converts a load hook into an application processor.

        >>> app = auto_application()
        >>> def f(): "something done before handling request"
        ...
        >>> app.add_processor(loadhook(f))
    """

    def processor(handler):
        h()
        return handler()

    return processor


def unloadhook(h):
    """
    【功能层】将一个"请求后"钩子函数 h 包装成处理器，无论请求是否抛出异常都会执行 h()。
    【设计层】对生成器响应（流式输出）做了特殊处理：wrap() 是一个生成器，
             它迭代原始结果，在迭代结束（StopIteration）时调用钩子，
             确保即使响应是流式的，卸载钩子也在所有数据发送后才执行。
             这类似 try/finally 的语义，但适配了生成器的惰性求值特性。
    【上下文层】application.__init__ 用此函数包装 _unload（将 app 出栈）。

    Converts an unload hook into an application processor.

        >>> app = auto_application()
        >>> def f(): "something done after handling request"
        ...
        >>> app.add_processor(unloadhook(f))
    """

    def processor(handler):
        try:
            result = handler()
        except:
            # run the hook even when handler raises some exception
            h()
            raise

        if result and hasattr(result, "__next__"):
            return wrap(result)
        else:
            h()
            return result

    def wrap(result):
        def next_hook():
            try:
                return next(result)
            except:
                # call the hook at the and of iterator
                h()
                raise

        result = iter(result)
        while True:
            try:
                yield next_hook()
            except StopIteration:
                return

    return processor


def autodelegate(prefix=""):
    """
    Returns a method that takes one argument and calls the method named prefix+arg,
    calling `notfound()` if there isn't one. Example:

        urls = ('/prefs/(.*)', 'prefs')

        class prefs:
            GET = autodelegate('GET_')
            def GET_password(self): pass
            def GET_privacy(self): pass

    `GET_password` would get called for `/prefs/password` while `GET_privacy` for
    `GET_privacy` gets called for `/prefs/privacy`.

    If a user visits `/prefs/password/change` then `GET_password(self, '/change')`
    is called.
    """

    def internal(self, arg):
        if "/" in arg:
            first, rest = arg.split("/", 1)
            func = prefix + first
            args = ["/" + rest]
        else:
            func = prefix + arg
            args = []

        if hasattr(self, func):
            try:
                return getattr(self, func)(*args)
            except TypeError:
                raise web.notfound()
        else:
            raise web.notfound()

    return internal


class Reloader:
    """
    【功能层】文件变更检测器：检查所有已加载模块的源文件修改时间，
             有变更则自动 reload，实现开发时热重载。
    【设计层】通过记录各模块的 mtime（上次修改时间）与当前 mtime 对比，
             利用 importlib.reload() 重新加载变更模块。
             实现了 __call__ 协议，可作为 loadhook 的参数直接传递给处理器链。
    【上下文层】debug 模式下由 application.__init__ 注册到 loadhook，
             每次请求前触发检查，无需重启服务即可生效代码变更。

    Checks to see if any loaded modules have changed on disk and,
    if so, reloads them.
    """

    """File suffix of compiled modules."""
    if sys.platform.startswith("java"):
        SUFFIX = "$py.class"
    else:
        SUFFIX = ".pyc"

    def __init__(self):
        self.mtimes = {}

    def __call__(self):
        sys_modules = list(sys.modules.values())
        for mod in sys_modules:
            self.check(mod)

    def check(self, mod):
        # jython registers java packages as modules but they either
        # don't have a __file__ attribute or its value is None
        if not (mod and hasattr(mod, "__file__") and mod.__file__):
            return

        try:
            mtime = os.stat(mod.__file__).st_mtime
        except OSError:
            return
        if mod.__file__.endswith(self.__class__.SUFFIX) and os.path.exists(
            mod.__file__[:-1]
        ):
            mtime = max(os.stat(mod.__file__[:-1]).st_mtime, mtime)

        if mod not in self.mtimes:
            self.mtimes[mod] = mtime
        elif self.mtimes[mod] < mtime:
            try:
                reload(mod)
                self.mtimes[mod] = mtime
            except ImportError:
                pass


if __name__ == "__main__":
    import doctest

    doctest.testmod()
