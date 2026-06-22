"""
Web API（WSGI 上层封装）
(from web.py)

【功能层】向用户代码暴露 HTTP 语义：读取请求输入、设置响应头/Cookie、
         抛出 HTTP 状态码异常、访问请求上下文 ctx。
【设计层】用异常（HTTPError 及其子类）来表达 HTTP 响应状态，
         利用 threadeddict 实现每请求独立的上下文对象（ctx），
         动态创建 HTTP 状态码类（_status_code 工厂函数），
         体现了 Python 元编程与异常控制流的惯用法。
【上下文层】是 web.py 对外 API 的核心层，应用开发者几乎所有操作
         都通过此模块的函数完成，被所有其他模块依赖。
"""

import pprint
import sys
import urllib
from http.cookies import CookieError, Morsel, SimpleCookie
from urllib.parse import parse_qs, quote, unquote, urljoin

import multipart

from .utils import dictadd, intget, safestr, storage, storify, threadeddict

__all__ = [
    "config",
    "header",
    "debug",
    "input",
    "data",
    "setcookie",
    "cookies",
    "ctx",
    "HTTPError",
    # 200, 201, 202, 204
    "OK",
    "Created",
    "Accepted",
    "NoContent",
    "ok",
    "created",
    "accepted",
    "nocontent",
    # 301, 302, 303, 304, 307
    "Redirect",
    "Found",
    "SeeOther",
    "NotModified",
    "TempRedirect",
    "redirect",
    "found",
    "seeother",
    "notmodified",
    "tempredirect",
    # 400, 401, 403, 404, 405, 406, 409, 410, 412, 415, 451
    "BadRequest",
    "Unauthorized",
    "Forbidden",
    "NotFound",
    "NoMethod",
    "NotAcceptable",
    "Conflict",
    "Gone",
    "PreconditionFailed",
    "UnsupportedMediaType",
    "UnavailableForLegalReasons",
    "badrequest",
    "unauthorized",
    "forbidden",
    "notfound",
    "nomethod",
    "notacceptable",
    "conflict",
    "gone",
    "preconditionfailed",
    "unsupportedmediatype",
    "unavailableforlegalreasons",
    # 500
    "InternalError",
    "internalerror",
]

config = storage()
config.__doc__ = """
【功能层】全局配置对象，控制框架运行时行为（调试模式、数据库连接、邮件服务器等）。
【设计层】使用 Storage 实例作为配置载体，既可 config.debug 也可 config['debug']，
         比 dict 更直观，比 dataclass 更灵活（运行时可任意增加键）。
【上下文层】各模块通过 `from .webapi import config` 共享同一全局配置实例；
         用户代码在启动时设置 web.config.debug = True 等即可改变框架行为。

A configuration object for various aspects of web.py.

`debug`
   : when True, enables reloading, disabled template caching and sets internalerror to debugerror.
"""


class HTTPError(Exception):
    """
    【功能层】所有 HTTP 响应异常的基类。抛出此异常即可终止正常处理流程，
             直接向客户端返回对应的 HTTP 状态码和响应体。
    【设计层】用异常（而非返回值）表达 HTTP 响应，是 web.py 的核心设计选择。
             好处：可在任意调用深度（模板、辅助函数内）直接终止请求，
             无需逐层传递返回值。本质是"以异常为控制流"的编程范式。
    【上下文层】application.wsgifunc 中的 except web.HTTPError as e 捕获所有子类，
             统一转换为 WSGI 响应，是框架请求处理管道的终止点。
    """
    def __init__(self, status, headers={}, data=""):
        ctx.status = status             # 写入请求上下文的状态码（如 "404 Not Found"）
        for k, v in headers.items():
            header(k, v)               # 逐个添加响应头到 ctx.headers
        self.data = data               # 响应体内容
        Exception.__init__(self, status)


def _status_code(status, data=None, classname=None, docstring=None):
    """
    【功能层】HTTP 状态码类的动态工厂函数，根据状态码字符串（如 "200 OK"）
             在运行时动态创建对应的异常类（如 OK、Created 等）。
    【设计层】使用 type(name, bases, dict) 元类调用动态创建类——这是 Python
             元编程最基础的形式，避免了为每个状态码手写重复的类定义。
             每个生成的类都继承 HTTPError，可被 except HTTPError 统一捕获。
    【上下文层】模块级别调用此函数批量生成 ok/OK、created/Created 等别名，
             让用户可以 `raise web.NotFound()` 或 `return web.ok()` 两种风格混用。
    """
    if data is None:
        data = status.split(" ", 1)[1]    # 从 "404 Not Found" 提取 "Not Found" 作为默认响应体
    classname = status.split(" ", 1)[1].replace(
        " ", ""
    )  # "304 Not Modified" -> "NotModified"（类名不含空格）
    docstring = docstring or "`%s` status" % status

    def __init__(self, data=data, headers={}):
        HTTPError.__init__(self, status, headers, data)

    # 【设计层】type() 三参数调用：type(类名, 基类元组, 属性字典) 动态创建类
    return type(
        classname, (HTTPError, object), {"__doc__": docstring, "__init__": __init__}
    )


ok = OK = _status_code("200 OK", data="")
created = Created = _status_code("201 Created")
accepted = Accepted = _status_code("202 Accepted")
nocontent = NoContent = _status_code("204 No Content")


class Redirect(HTTPError):
    """A `301 Moved Permanently` redirect."""

    def __init__(self, url, status="301 Moved Permanently", absolute=False):
        """
        Returns a `status` redirect to the new URL.
        `url` is joined with the base URL so that things like
        `redirect("about") will work properly.
        """
        newloc = urljoin(ctx.path, url)

        if newloc.startswith("/"):
            if absolute:
                home = ctx.realhome
            else:
                home = ctx.home
            newloc = home + newloc

        headers = {"Content-Type": "text/html", "Location": newloc}
        HTTPError.__init__(self, status, headers, "")


redirect = Redirect


class Found(Redirect):
    """A `302 Found` redirect."""

    def __init__(self, url, absolute=False):
        Redirect.__init__(self, url, "302 Found", absolute=absolute)


found = Found


class SeeOther(Redirect):
    """A `303 See Other` redirect."""

    def __init__(self, url, absolute=False):
        Redirect.__init__(self, url, "303 See Other", absolute=absolute)


seeother = SeeOther


class NotModified(HTTPError):
    """A `304 Not Modified` status."""

    def __init__(self):
        HTTPError.__init__(self, "304 Not Modified")


notmodified = NotModified


class TempRedirect(Redirect):
    """A `307 Temporary Redirect` redirect."""

    def __init__(self, url, absolute=False):
        Redirect.__init__(self, url, "307 Temporary Redirect", absolute=absolute)


tempredirect = TempRedirect


class BadRequest(HTTPError):
    """`400 Bad Request` error."""

    message = "bad request"

    def __init__(self, message=None):
        status = "400 Bad Request"
        headers = {"Content-Type": "text/html"}
        HTTPError.__init__(self, status, headers, message or self.message)


badrequest = BadRequest


class Unauthorized(HTTPError):
    """`401 Unauthorized` error."""

    message = "unauthorized"

    def __init__(self, message=None):
        status = "401 Unauthorized"
        headers = {"Content-Type": "text/html"}
        HTTPError.__init__(self, status, headers, message or self.message)


unauthorized = Unauthorized


class Forbidden(HTTPError):
    """`403 Forbidden` error."""

    message = "forbidden"

    def __init__(self, message=None):
        status = "403 Forbidden"
        headers = {"Content-Type": "text/html"}
        HTTPError.__init__(self, status, headers, message or self.message)


forbidden = Forbidden


class _NotFound(HTTPError):
    """`404 Not Found` error."""

    message = "not found"

    def __init__(self, message=None):
        status = "404 Not Found"
        headers = {"Content-Type": "text/html; charset=utf-8"}
        HTTPError.__init__(self, status, headers, message or self.message)


def NotFound(message=None):
    """Returns HTTPError with '404 Not Found' error from the active application."""
    if message:
        return _NotFound(message)
    elif ctx.get("app_stack"):
        return ctx.app_stack[-1].notfound()
    else:
        return _NotFound()


notfound = NotFound


class NoMethod(HTTPError):
    """A `405 Method Not Allowed` error."""

    message = "method not allowed"

    def __init__(self, cls=None):
        status = "405 Method Not Allowed"
        headers = {}
        headers["Content-Type"] = "text/html"

        methods = ["GET", "HEAD", "POST", "PUT", "DELETE"]
        if cls:
            methods = [method for method in methods if hasattr(cls, method)]

        headers["Allow"] = ", ".join(methods)
        HTTPError.__init__(self, status, headers, self.message)


nomethod = NoMethod


class NotAcceptable(HTTPError):
    """`406 Not Acceptable` error."""

    message = "not acceptable"

    def __init__(self, message=None):
        status = "406 Not Acceptable"
        headers = {"Content-Type": "text/html"}
        HTTPError.__init__(self, status, headers, message or self.message)


notacceptable = NotAcceptable


class Conflict(HTTPError):
    """`409 Conflict` error."""

    message = "conflict"

    def __init__(self, message=None):
        status = "409 Conflict"
        headers = {"Content-Type": "text/html"}
        HTTPError.__init__(self, status, headers, message or self.message)


conflict = Conflict


class Gone(HTTPError):
    """`410 Gone` error."""

    message = "gone"

    def __init__(self, message=None):
        status = "410 Gone"
        headers = {"Content-Type": "text/html"}
        HTTPError.__init__(self, status, headers, message or self.message)


gone = Gone


class PreconditionFailed(HTTPError):
    """`412 Precondition Failed` error."""

    message = "precondition failed"

    def __init__(self, message=None):
        status = "412 Precondition Failed"
        headers = {"Content-Type": "text/html"}
        HTTPError.__init__(self, status, headers, message or self.message)


preconditionfailed = PreconditionFailed


class UnsupportedMediaType(HTTPError):
    """`415 Unsupported Media Type` error."""

    message = "unsupported media type"

    def __init__(self, message=None):
        status = "415 Unsupported Media Type"
        headers = {"Content-Type": "text/html"}
        HTTPError.__init__(self, status, headers, message or self.message)


unsupportedmediatype = UnsupportedMediaType


class _UnavailableForLegalReasons(HTTPError):
    """`451 Unavailable For Legal Reasons` error."""

    message = "unavailable for legal reasons"

    def __init__(self, message=None):
        status = "451 Unavailable For Legal Reasons"
        headers = {"Content-Type": "text/html"}
        HTTPError.__init__(self, status, headers, message or self.message)


def UnavailableForLegalReasons(message=None):
    """Returns HTTPError with '415 Unavailable For Legal Reasons' error from the active application."""
    if message:
        return _UnavailableForLegalReasons(message)
    elif ctx.get("app_stack"):
        return ctx.app_stack[-1].unavailableforlegalreasons()
    else:
        return _UnavailableForLegalReasons()


unavailableforlegalreasons = UnavailableForLegalReasons


class _InternalError(HTTPError):
    """500 Internal Server Error`."""

    message = "internal server error"

    def __init__(self, message=None):
        status = "500 Internal Server Error"
        headers = {"Content-Type": "text/html"}
        HTTPError.__init__(self, status, headers, message or self.message)


def InternalError(message=None):
    """Returns HTTPError with '500 internal error' error from the active application."""
    if message:
        return _InternalError(message)
    elif ctx.get("app_stack"):
        return ctx.app_stack[-1].internalerror()
    else:
        return _InternalError()


internalerror = InternalError


def header(hdr, value, unique=False):
    """
    【功能层】向当前请求的响应头列表中追加一条响应头。
    【设计层】unique=True 时先遍历现有头，同名则跳过，防止重复；
             响应头以列表形式存储（支持同名多值，如 Set-Cookie），
             而非字典（字典同名键会覆盖）。同时做换行符注入检测，
             防御 HTTP 响应拆分攻击（Response Splitting Attack）。
    【上下文层】写入 ctx.headers，最终由 application.wsgifunc 中的
             start_response(status, headers) 传给 WSGI 服务器。

    Adds the header `hdr: value` with the response.

    If `unique` is True and a header with that name already exists,
    it doesn't add a new one.
    """
    hdr, value = safestr(hdr), safestr(value)
    # protection against HTTP response splitting attack
    if "\n" in hdr or "\r" in hdr or "\n" in value or "\r" in value:
        raise ValueError("invalid characters in header")
    if unique is True:
        for h, v in ctx.headers:
            if h.lower() == hdr.lower():
                return

    ctx.headers.append((hdr, value))


def rawinput(method=None):
    """Returns storage object with GET or POST arguments."""
    method = method or "both"

    def dictify(fs):
        return {k: fs[k] for k in fs}

    env = ctx.env.copy()
    post_req = get_req = {}

    if method.lower() in ["both", "post", "put", "patch"]:
        if env["REQUEST_METHOD"] in ["POST", "PUT", "PATCH"]:
            if env.get("CONTENT_TYPE", "").lower().startswith("multipart/"):
                # since wsgi.input is directly passed to multipart,
                # it can not be called multiple times. Saving the result
                # object in ctx to allow calling web.input multiple times.
                post_req = ctx.get(
                    "_fieldstorage"
                )  # TODO: Rename? is this visible anywhere else?
                if not post_req:
                    try:
                        # This returns two dicts, forms & files.
                        forms, files = multipart.parse_form_data(environ=env)
                        post_req = dictadd(forms, files)
                        ctx._fieldstorage = post_req
                    except IndexError:
                        post_req = {}

            else:
                post_data = data().decode("utf-8")
                post_req = parse_qs(post_data, keep_blank_values=True)
            post_req = dictify(post_req)

    if method.lower() in ["both", "get"]:
        env["REQUEST_METHOD"] = "GET"
        get_req = dict(
            urllib.parse.parse_qs(env.get("QUERY_STRING", ""), keep_blank_values=True)
        )

    def process_values(values):
        if isinstance(values, list):
            return [process_values(x) for x in values]
        elif hasattr(values, "filename") and values.filename is None:
            return values.value
        else:
            return values

    return storage(
        [(k, process_values(v)) for k, v in dictadd(get_req, post_req).items()]
    )


def input(*requireds, **defaults):
    """
    【功能层】返回包含当前请求 GET/POST 参数的 Storage 对象，
             支持声明必填字段（requireds）和默认值（defaults）。
    【设计层】先调用 rawinput() 获取原始参数字典，再用 storify() 转换为
             可属性访问的 Storage；若缺少必填字段自动返回 400 BadRequest。
             _method 参数允许只读取 GET 或 POST 参数子集。
    【上下文层】应用开发者最常用的 API 之一：`data = web.input(name="default")`，
             内部依赖 rawinput、storify 和 multipart 库完成解析。

    Returns a `storage` object with the GET and POST arguments.
    See `storify` for how `requireds` and `defaults` work.
    """
    _method = defaults.pop("_method", "both")
    out = rawinput(_method)
    try:
        defaults.setdefault("_unicode", True)  # force unicode conversion by default.
        return storify(out, *requireds, **defaults)
    except KeyError:
        raise badrequest()


def data():
    """Returns the data sent with the request."""
    if "data" not in ctx:
        if ctx.env.get("HTTP_TRANSFER_ENCODING") == "chunked":
            ctx.data = ctx.env["wsgi.input"].read()
        else:
            cl = intget(ctx.env.get("CONTENT_LENGTH"), 0)
            ctx.data = ctx.env["wsgi.input"].read(cl)
    return ctx.data


def setcookie(
    name,
    value,
    expires="",
    domain=None,
    secure=False,
    httponly=False,
    path=None,
    samesite=None,
):
    """Sets a cookie."""
    morsel = Morsel()
    name, value = safestr(name), safestr(value)
    morsel.set(name, value, quote(value))
    if isinstance(expires, int) and expires < 0:
        expires = -1000000000
    morsel["expires"] = expires
    morsel["path"] = path or ctx.homepath + "/"
    if domain:
        morsel["domain"] = domain
    if secure:
        morsel["secure"] = secure
    if httponly:
        morsel["httponly"] = True
    value = morsel.OutputString()
    if samesite and samesite.lower() in ("strict", "lax", "none"):
        value += "; SameSite=%s" % samesite
    header("Set-Cookie", value)


def parse_cookies(http_cookie):
    r"""Parse a HTTP_COOKIE header and return dict of cookie names and decoded values.

    >>> sorted(parse_cookies('').items())
    []
    >>> sorted(parse_cookies('a=1').items())
    [('a', '1')]
    >>> sorted(parse_cookies('a=1%202').items())
    [('a', '1 2')]
    >>> sorted(parse_cookies('a=Z%C3%A9Z').items())
    [('a', 'Z\xc3\xa9Z')]
    >>> sorted(parse_cookies('a=1; b=2; c=3').items())
    [('a', '1'), ('b', '2'), ('c', '3')]

    # TODO: cclauss re-enable this test
    # >>> sorted(parse_cookies('a=1; b=w("x")|y=z; c=3').items())
    # [('a', '1'), ('b', 'w('), ('c', '3')]

    >>> sorted(parse_cookies('a=1; b=w(%22x%22)|y=z; c=3').items())
    [('a', '1'), ('b', 'w("x")|y=z'), ('c', '3')]

    >>> sorted(parse_cookies('keebler=E=mc2').items())
    [('keebler', 'E=mc2')]
    >>> sorted(parse_cookies(r'keebler="E=mc2; L=\"Loves\"; fudge=\012;"').items())
    [('keebler', 'E=mc2; L="Loves"; fudge=\n;')]
    """
    # print "parse_cookies"
    if '"' in http_cookie:
        # HTTP_COOKIE has quotes in it, use slow but correct cookie parsing
        cookie = SimpleCookie()
        try:
            cookie.load(http_cookie)
        except CookieError:
            # If HTTP_COOKIE header is malformed, try at least to load the cookies we can by
            # first splitting on ';' and loading each attr=value pair separately
            cookie = SimpleCookie()
            for attr_value in http_cookie.split(";"):
                try:
                    cookie.load(attr_value)
                except CookieError:
                    pass
        cookies = {k: unquote(v.value) for k, v in cookie.items()}
    else:
        # HTTP_COOKIE doesn't have quotes, use fast cookie parsing
        cookies = {}
        for key_value in http_cookie.split(";"):
            key_value = key_value.split("=", 1)
            if len(key_value) == 2:
                key, value = key_value
                cookies[key.strip()] = unquote(value.strip())
    return cookies


def cookies(*requireds, **defaults):
    """Returns a `storage` object with all the request cookies in it.

    See `storify` for how `requireds` and `defaults` work.

    This is forgiving on bad HTTP_COOKIE input, it tries to parse at least
    the cookies it can.

    The values are converted to unicode if _unicode=True is passed.
    """
    # parse cookie string and cache the result for next time.
    if "_parsed_cookies" not in ctx:
        http_cookie = ctx.env.get("HTTP_COOKIE", "")
        ctx._parsed_cookies = parse_cookies(http_cookie)

    try:
        return storify(ctx._parsed_cookies, *requireds, **defaults)
    except KeyError:
        badrequest()
        raise StopIteration()


def debug(*args):
    """
    Prints a prettyprinted version of `args` to stderr.
    """
    try:
        out = ctx.environ["wsgi.errors"]
    except:
        out = sys.stderr
    for arg in args:
        print(pprint.pformat(arg), file=out)
    return ""


def _debugwrite(x):
    try:
        out = ctx.environ["wsgi.errors"]
    except:
        out = sys.stderr
    out.write(x)


debug.write = _debugwrite

ctx = context = threadeddict()
# 【功能层】ctx 是每个请求的上下文对象，存储请求的所有相关信息（path、method、env 等）
#          和响应的状态（status、headers、output）。
# 【设计层】threadeddict() 即 ThreadedDict 实例，利用 threading.local 实现线程隔离：
#          不同线程（请求）读写各自的 ctx 互不干扰，无需加锁。
#          同时提供 `context` 作为别名，两者指向同一对象。
# 【上下文层】框架所有组件（application.load、webapi 各函数、session 等）都通过
#          `web.ctx` 读写请求状态，它是贯穿整个请求生命周期的"全局请求状态容器"。

ctx.__doc__ = """
A `storage` object containing various information about the request:

`environ` (aka `env`)
   : A dictionary containing the standard WSGI environment variables.

`host`
   : The domain (`Host` header) requested by the user.

`home`
   : The base path for the application.

`ip`
   : The IP address of the requester.

`method`
   : The HTTP method used.

`path`
   : The path request.

`query`
   : If there are no query arguments, the empty string. Otherwise, a `?` followed
     by the query string.

`fullpath`
   : The full path requested, including query arguments (`== path + query`).

### Response Data

`status` (default: "200 OK")
   : The status code to be used in the response.

`headers`
   : A list of 2-tuples to be used in the response.

`output`
   : A string to be used as the response.
"""

if __name__ == "__main__":
    import doctest

    doctest.testmod()
