"""
HTTP Utilities（HTTP 工具函数集）
(from web.py)

【功能层】提供 HTTP 缓存控制（Expires/Last-Modified/ETag）、URL 构建与修改、
         性能分析输出等工具函数。
【设计层】所有函数依赖 web.ctx 读取请求上下文，体现了"隐式上下文"设计——
         调用方无需显式传递请求对象，框架通过线程本地变量自动提供。
【上下文层】应用开发者在视图函数中调用，如 `web.expires(3600)` 设置缓存，
         `web.url('/path', key='val')` 构建带参数 URL。
"""

__all__ = [
    "expires",
    "lastmodified",
    "prefixurl",
    "modified",
    "changequery",
    "url",
    "profiler",
]

import datetime
from urllib.parse import urlencode as urllib_urlencode

from . import net, utils
from . import webapi as web
from .py3helpers import iteritems


def prefixurl(base=""):
    """
    Sorry, this function is really difficult to explain.
    Maybe some other time.
    """
    url = web.ctx.path.lstrip("/")
    for i in range(url.count("/")):
        base += "../"
    if not base:
        base = "./"
    return base


def expires(delta):
    """
    Outputs an `Expires` header for `delta` from now.
    `delta` is a `timedelta` object or a number of seconds.
    """
    if isinstance(delta, int):
        delta = datetime.timedelta(seconds=delta)
    date_obj = datetime.datetime.utcnow() + delta
    web.header("Expires", net.httpdate(date_obj))


def lastmodified(date_obj):
    """Outputs a `Last-Modified` header for `datetime`."""
    web.header("Last-Modified", net.httpdate(date_obj))


def modified(date=None, etag=None):
    """
    【功能层】HTTP 缓存协商：检查客户端缓存是否仍然有效，若有效则抛出 304 Not Modified，
             避免重复传输未变化的内容。
    【设计层】同时支持 Last-Modified（时间戳）和 ETag（版本令牌）两种缓存验证机制，
             任一匹配即视为缓存有效。时间比较时减去 1 秒是因为 HTTP 日期精度只到秒级。
             用"抛出异常"而非"返回布尔值"来终止请求，保持与 HTTPError 体系一致。
    【上下文层】视图函数中使用：`if web.modified(date=last_change): ...`，
             配合 expires() 和 lastmodified() 构建完整的 HTTP 缓存策略。

    Checks to see if the page has been modified since the version in the
    requester's cache.

    When you publish pages, you can include `Last-Modified` and `ETag`
    with the date the page was last modified and an opaque token for
    the particular version, respectively. When readers reload the page,
    the browser sends along the modification date and etag value for
    the version it has in its cache. If the page hasn't changed,
    the server can just return `304 Not Modified` and not have to
    send the whole page again.

    This function takes the last-modified date `date` and the ETag `etag`
    and checks the headers to see if they match. If they do, it returns
    `True`, or otherwise it raises NotModified error. It also sets
    `Last-Modified` and `ETag` output headers.
    """
    n = {x.strip('" ') for x in web.ctx.env.get("HTTP_IF_NONE_MATCH", "").split(",")}
    m = net.parsehttpdate(web.ctx.env.get("HTTP_IF_MODIFIED_SINCE", "").split(";")[0])
    validate = False
    if etag:
        if "*" in n or etag in n:
            validate = True
    if date and m:
        # we subtract a second because
        # HTTP dates don't have sub-second precision
        if date - datetime.timedelta(seconds=1) <= m:
            validate = True

    if date:
        lastmodified(date)
    if etag:
        web.header("ETag", '"' + etag + '"')
    if validate:
        raise web.notmodified()
    else:
        return True


def urlencode(query, doseq=0):
    """
    Same as urllib.urlencode, but supports unicode strings.

        >>> urlencode({'text':'foo bar'})
        'text=foo+bar'
        >>> urlencode({'x': [1, 2]}, doseq=True)
        'x=1&x=2'
    """

    def convert(value, doseq=False):
        if doseq and isinstance(value, list):
            return [convert(v) for v in value]
        else:
            return utils.safestr(value)

    query = {k: convert(v, doseq) for k, v in query.items()}
    return urllib_urlencode(query, doseq=doseq)


def changequery(query=None, **kw):
    """
    【功能层】在保留当前 URL 其他查询参数的基础上，修改/删除指定参数，返回新 URL。
    【设计层】先读取当前所有 GET 参数（rawinput），再用 **kw 覆盖或删除（value=None）
             指定键，最后重新编码。此模式避免了手工解析 URL 的繁琐，
             是"不可变更新"（immutable update）风格在 URL 操作上的应用。
    【上下文层】用于分页、排序、筛选等场景：`web.changequery(page=2)` 在当前 URL
             基础上只改变 page 参数，其他参数自动保留。

    Imagine you're at `/foo?a=1&b=2`. Then `changequery(a=3)` will return
    `/foo?a=3&b=2` -- the same URL but with the arguments you requested
    changed.
    """
    if query is None:
        query = web.rawinput(method="get")
    for k, v in iteritems(kw):
        if v is None:
            query.pop(k, None)
        else:
            query[k] = v
    out = web.ctx.path
    if query:
        out += "?" + urlencode(query, doseq=True)
    return out


def url(path=None, doseq=False, **kw):
    """
    Makes url by concatenating web.ctx.homepath and path and the
    query string created using the arguments.
    """
    if path is None:
        path = web.ctx.path
    if path.startswith("/"):
        out = web.ctx.homepath + path
    else:
        out = path

    if kw:
        out += "?" + urlencode(kw, doseq=doseq)

    return out


def profiler(app):
    """Outputs basic profiling information at the bottom of each response."""
    from utils import profile

    def profile_internal(e, o):
        out, result = profile(app)(e, o)
        return list(out) + ["<pre>" + net.websafe(result) + "</pre>"]

    return profile_internal


if __name__ == "__main__":
    import doctest

    doctest.testmod()
