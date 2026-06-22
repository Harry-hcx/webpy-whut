"""
Session Management（会话管理）
(from web.py)

【功能层】实现 HTTP 会话：在无状态的 HTTP 协议上维护跨请求的用户状态，
         支持磁盘（DiskStore）、数据库（DBStore）、内存（MemoryStore）三种存储后端。
【设计层】Session 类用 __slots__ 限制实例属性，防止误操作；
         通过 threadeddict 实现每请求独立的会话数据存储；
         用 pickle + base64 序列化会话数据，支持任意 Python 对象存储；
         SHA1 哈希 + 随机字节生成会话 ID，防止猜测攻击。
         存储后端抽象为 Store 基类 + 具体实现，是"策略模式"（Strategy Pattern）。
【上下文层】通过 app.add_processor 注入请求处理管道，对每个请求透明地加载/保存会话。
"""

import datetime
import os
import os.path
import pickle
import shutil
import threading
import time
from base64 import decodebytes, encodebytes
from copy import deepcopy
from hashlib import sha1

from . import utils
from . import webapi as web
from .py3helpers import iteritems

__all__ = ["Session", "SessionExpired", "Store", "DiskStore", "DBStore", "MemoryStore"]

web.config.session_parameters = utils.storage(
    {
        "cookie_name": "webpy_session_id",   # Cookie 键名
        "cookie_domain": None,               # Cookie 域，None 表示当前域
        "cookie_path": None,                 # Cookie 路径
        "samesite": None,                    # SameSite 策略（None/Lax/Strict）
        "timeout": 86400,  # 24 * 60 * 60, # 会话超时秒数（默认 24 小时）
        "ignore_expiry": True,               # True：会话过期时静默重置，False：抛出 SessionExpired
        "ignore_change_ip": True,            # True：允许 IP 变化，False：IP 变化时使会话失效
        "secret_key": "fLjUfxqXtfNoIldA0A0J",  # 生成会话 ID 时的 HMAC 盐值，生产环境务必修改
        "expired_message": "Session expired",   # 会话过期提示消息
        "httponly": True,                    # HttpOnly 标志：阻止 JS 读取 Cookie，防 XSS
        "secure": False,                     # Secure 标志：仅 HTTPS 传输 Cookie
    }
)


class SessionExpired(web.HTTPError):
    def __init__(self, message):
        web.HTTPError.__init__(self, "200 OK", {}, data=message)


class Session:
    """
    【功能层】HTTP 会话管理器：在请求间持久化用户状态（登录信息、购物车等）。
    【设计层】使用 __slots__ 明确限制实例属性，防止用户误将普通属性写入 Session 实例
             而非会话数据；通过 __getattr__/__setattr__ 将属性访问透明代理到
             底层 _data（ThreadedDict 实例），使 session.username 等同于线程安全的字典访问。
             _processor 作为 WSGI 处理器注入，实现 AOP（面向切面）风格的会话管理。
    【上下文层】典型用法：`session = Session(app, DiskStore('sessions'))`，
             之后在视图函数中直接读写 `session.username` 即可。

    Session management for web.py"""

    __slots__ = [
        "store",             # 存储后端实例（DiskStore/DBStore/MemoryStore）
        "_initializer",      # 会话初始化器：dict 或 callable，新会话时调用
        "_last_cleanup_time",# 上次清理过期会话的时间戳，避免每次请求都清理
        "_config",           # 会话配置副本（Storage 对象）
        "_data",             # 线程本地会话数据容器（ThreadedDict）
        "__getitem__",       # 代理到 _data 的下标访问
        "__setitem__",       # 代理到 _data 的下标写入
        "__delitem__",       # 代理到 _data 的下标删除
    ]

    def __init__(self, app, store, initializer=None):
        self.store = store
        self._initializer = initializer
        self._last_cleanup_time = 0
        self._config = utils.storage(web.config.session_parameters)  # 独立副本，允许单实例覆盖
        self._data = utils.threadeddict()   # 线程本地存储，不同请求线程各自独立

        # 【设计层】将 _data 的下标操作直接绑定到 Session 实例属性，
        #          避免 __setattr__ 拦截后的额外开销，也方便 session['key'] 语法
        self.__getitem__ = self._data.__getitem__
        self.__setitem__ = self._data.__setitem__
        self.__delitem__ = self._data.__delitem__

        if app:
            app.add_processor(self._processor)  # 将会话管理注入到请求处理链

    def __contains__(self, name):
        return name in self._data

    def __getattr__(self, name):
        return getattr(self._data, name)

    def __setattr__(self, name, value):
        # 【设计层】__slots__ 中声明的属性直接写入对象本身（object.__setattr__），
        #          其他属性透明代理到 _data（线程本地会话字典）
        #          这保证了 self.store = store 和 session.username = "alice" 语义不同
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._data, name, value)

    def __delattr__(self, name):
        delattr(self._data, name)

    def _processor(self, handler):
        """
        【功能层】WSGI 处理器：在每次请求前加载会话，请求后保存会话（finally 保证必执行）。
        【设计层】符合处理器协议（接受 handler，返回响应），用 try/finally 确保
                 无论请求是否异常，_save() 都会执行，避免会话数据丢失。
        【上下文层】由 __init__ 通过 app.add_processor 注册，对应用代码完全透明。
        """
        self._cleanup()   # 定期清理过期会话（按 timeout 间隔）
        self._load()      # 从 Cookie 读取会话 ID，从 store 恢复会话数据

        try:
            return handler()
        finally:
            self._save()  # 无论成功还是异常，都将会话数据写回存储

    def _load(self):
        """Load the session from the store, by the id from cookie"""
        cookie_name = self._config.cookie_name
        self.session_id = web.cookies().get(cookie_name)
        # Handler can do session.send_cookie = False to not send the cookie
        self.send_cookie = True

        # protection against session_id tampering
        if self.session_id and not self._valid_session_id(self.session_id):
            self.session_id = None

        self._check_expiry()
        if self.session_id:
            d = self.store[self.session_id]
            self.update(d)
            self._validate_ip()

        if not self.session_id:
            self.session_id = self._generate_session_id()

            if self._initializer:
                if isinstance(self._initializer, dict):
                    self.update(deepcopy(self._initializer))
                elif hasattr(self._initializer, "__call__"):
                    self._initializer()

        self.ip = web.ctx.ip

    def _check_expiry(self):
        # check for expiry
        if self.session_id and self.session_id not in self.store:
            if self._config.ignore_expiry:
                self.session_id = None
            else:
                return self.expired()

    def _validate_ip(self):
        # check for change of IP
        if self.session_id and self.get("ip", None) != web.ctx.ip:
            if not self._config.ignore_change_ip:
                return self.expired()

    def _save(self):
        current_values = dict(self._data)
        del current_values["session_id"]
        del current_values["ip"]
        if not self.send_cookie:
            return
        if not self.get("_killed"):
            self._setcookie(self.session_id)
            self.store[self.session_id] = dict(self._data)
        else:
            if web.cookies().get(self._config.cookie_name):
                self._setcookie(self.session_id, expires=-1)

    def _setcookie(self, session_id, expires="", **kw):
        cookie_name = self._config.cookie_name
        cookie_domain = self._config.cookie_domain
        cookie_path = self._config.cookie_path
        httponly = self._config.httponly
        secure = self._config.secure
        samesite = kw.get("samesite", self._config.get("samesite", None))
        web.setcookie(
            cookie_name,
            session_id,
            expires=expires,
            domain=cookie_domain,
            httponly=httponly,
            secure=secure,
            path=cookie_path,
            samesite=samesite,
        )

    def _generate_session_id(self):
        """
        【功能层】生成全局唯一的会话 ID（40 位十六进制 SHA1 哈希）。
        【设计层】组合 os.urandom(16)（密码学随机字节）、当前时间戳、
                 客户端 IP 和 secret_key 四个要素生成哈希，
                 大幅提高碰撞和预测难度；循环直到找到存储中不存在的 ID，
                 保证唯一性（极低概率需要重试）。
        【上下文层】新会话初始化时调用，生成的 ID 写入 Cookie 发送给客户端。
        """

        while True:
            rand = os.urandom(16)          # 16 字节强随机数，防止预测
            now = time.time()
            secret_key = self._config.secret_key

            hashable = f"{rand}{now}{utils.safestr(web.ctx.ip)}{secret_key}"
            session_id = sha1(hashable.encode("utf-8")).hexdigest()
            if session_id not in self.store:  # 确保唯一性
                break
        return session_id

    def _valid_session_id(self, session_id):
        rx = utils.re_compile("^[0-9a-fA-F]+$")
        return rx.match(session_id)

    def _cleanup(self):
        """Cleanup the stored sessions"""
        current_time = time.time()
        timeout = self._config.timeout
        if current_time - self._last_cleanup_time > timeout:
            self.store.cleanup(timeout)
            self._last_cleanup_time = current_time

    def expired(self):
        """Called when an expired session is atime"""
        self._killed = True
        self._save()
        raise SessionExpired(self._config.expired_message)

    def kill(self):
        """Kill the session, make it no longer available"""
        del self.store[self.session_id]
        self._killed = True


class Store:
    """
    【功能层】会话存储后端的抽象基类，定义存储接口规范。
    【设计层】使用抽象方法（NotImplementedError）而非 ABC，保持简洁；
             encode/decode 方法用 pickle + base64 序列化任意 Python 对象，
             子类可复用，也可覆盖实现自定义序列化（如 JSON）。
    【上下文层】DiskStore、DBStore、MemoryStore 均继承此类并实现具体存储逻辑，
             Session 类通过 store 接口操作，无需关心底层存储介质（策略模式）。
    """

    def __contains__(self, key):
        raise NotImplementedError()

    def __getitem__(self, key):
        raise NotImplementedError()

    def __setitem__(self, key, value):
        raise NotImplementedError()

    def cleanup(self, timeout):
        """removes all the expired sessions"""
        raise NotImplementedError()

    def encode(self, session_dict):
        """【功能层】将会话字典序列化为 bytes：先 pickle，再 base64 编码（便于文本存储）"""
        pickled = pickle.dumps(session_dict)
        return encodebytes(pickled)

    def decode(self, session_data):
        """【功能层】反序列化会话数据：base64 解码后 unpickle 恢复字典"""
        if isinstance(session_data, str):
            session_data = session_data.encode()

        pickled = decodebytes(session_data)
        return pickle.loads(pickled)


class DiskStore(Store):
    """
    【功能层】基于文件系统的会话存储：每个会话 ID 对应一个文件，
             文件内容为 pickle+base64 序列化的会话数据。
    【设计层】写操作先写到 ".tmp" 临时文件，再 shutil.move（原子重命名），
             防止写入过程中崩溃导致会话文件损坏。
             cleanup 通过检查文件的 atime（最后访问时间）删除过期会话。
    【上下文层】适合单机部署；多机/多进程部署需换用 DBStore 或分布式存储。

    Store for saving a session on disk.

        >>> import tempfile
        >>> root = tempfile.mkdtemp()
        >>> s = DiskStore(root)
        >>> s['a'] = 'foo'
        >>> s['a']
        'foo'
        >>> time.sleep(0.01)
        >>> s.cleanup(0.01)
        >>> s['a']
        Traceback (most recent call last):
            ...
        KeyError: 'a'
    """

    def __init__(self, root):
        # if the storage root doesn't exists, create it.
        if not os.path.exists(root):
            os.makedirs(os.path.abspath(root))
        self.root = root

    def _get_path(self, key):
        if os.path.sep in key:
            raise ValueError("Bad key: %s" % repr(key))
        return os.path.join(self.root, key)

    def __contains__(self, key):
        path = self._get_path(key)
        return os.path.exists(path)

    def __getitem__(self, key):
        path = self._get_path(key)

        if os.path.exists(path):
            with open(path, "rb") as fh:
                pickled = fh.read()
            return self.decode(pickled)
        else:
            raise KeyError(key)

    def __setitem__(self, key, value):
        path = self._get_path(key)
        pickled = self.encode(value)
        try:
            tname = path + "." + threading.current_thread().name
            f = open(tname, "wb")
            try:
                f.write(pickled)
            finally:
                f.close()
                shutil.move(tname, path)  # atomary operation
        except OSError:
            pass

    def __delitem__(self, key):
        path = self._get_path(key)
        if os.path.exists(path):
            os.remove(path)

    def cleanup(self, timeout):
        if not os.path.isdir(self.root):
            return

        now = time.time()
        for f in os.listdir(self.root):
            path = self._get_path(f)
            atime = os.stat(path).st_atime
            if now - atime > timeout:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)


class DBStore(Store):
    """
    【功能层】基于数据库的会话存储，会话数据存入指定数据库表。
    【设计层】表中 atime 字段记录最后访问时间，cleanup 通过时间比较批量删除过期行；
             __setitem__ 实现"upsert"逻辑（存在则 UPDATE，不存在则 INSERT），
             encode 结果去掉 bytes 前缀 b"..." 再存储，确保 base64 字符串格式正确。
    【上下文层】适合多进程/多机部署，会话数据在数据库中共享，依赖 web.db 模块。

    Store for saving a session in database
    Needs a table with the following columns:

        session_id CHAR(128) UNIQUE NOT NULL,
        atime DATETIME NOT NULL default current_timestamp,
        data TEXT
    """

    def __init__(self, db, table_name):
        self.db = db
        self.table = table_name

    def __contains__(self, key):
        data = self.db.select(self.table, where="session_id=$key", vars=locals())
        return bool(list(data))

    def __getitem__(self, key):
        now = datetime.datetime.now()
        try:
            s = self.db.select(self.table, where="session_id=$key", vars=locals())[0]
            self.db.update(
                self.table, where="session_id=$key", atime=now, vars=locals()
            )
        except IndexError:
            raise KeyError(key)
        else:
            return self.decode(s.data)

    def __setitem__(self, key, value):
        # Remove the leading `b` of bytes object (`b"..."`), otherwise encoded
        # value is invalid base64 format.
        pickled = self.encode(value).decode()

        now = datetime.datetime.now()
        if key in self:
            self.db.update(
                self.table,
                where="session_id=$key",
                data=pickled,
                atime=now,
                vars=locals(),
            )
        else:
            self.db.insert(self.table, False, session_id=key, atime=now, data=pickled)

    def __delitem__(self, key):
        self.db.delete(self.table, where="session_id=$key", vars=locals())

    def cleanup(self, timeout):
        timeout = datetime.timedelta(
            timeout / (24.0 * 60 * 60)
        )  # timedelta takes numdays as arg
        last_allowed_time = datetime.datetime.now() - timeout
        self.db.delete(self.table, where="$last_allowed_time > atime", vars=locals())


class ShelfStore:
    """Store for saving session using `shelve` module.

        import shelve
        store = ShelfStore(shelve.open('session.shelf'))

    XXX: is shelve thread-safe?
    """

    def __init__(self, shelf):
        self.shelf = shelf

    def __contains__(self, key):
        return key in self.shelf

    def __getitem__(self, key):
        atime, v = self.shelf[key]
        self[key] = v  # update atime
        return v

    def __setitem__(self, key, value):
        self.shelf[key] = time.time(), value

    def __delitem__(self, key):
        try:
            del self.shelf[key]
        except KeyError:
            pass

    def cleanup(self, timeout):
        now = time.time()
        for k in self.shelf:
            atime, v = self.shelf[k]
            if now - atime > timeout:
                del self[k]


class MemoryStore(Store):
    """
    【功能层】基于内存字典的会话存储，数据存储在进程内存中，重启后丢失。
    【设计层】用 (time, value) 元组记录每条会话的最后访问时间，
             __getitem__ 读取时自动更新时间（实现 LRU 语义）；
             cleanup 收集过期 key 后统一删除，避免迭代时修改字典（会抛异常）。
    【上下文层】适合 Flash 存储受限设备或测试环境；不适合多进程/多机部署。

    Store for saving a session in memory.
    Useful where there is limited fs writes on the disk, like
    flash memories

    Data will be saved into a dict:
    k: (time, pydata)
    """

    def __init__(self, d_store=None):
        if d_store is None:
            d_store = {}
        self.d_store = d_store

    def __contains__(self, key):
        return key in self.d_store

    def __getitem__(self, key):
        """Return the value and update the last seen value"""
        t, value = self.d_store[key]
        self.d_store[key] = (time.time(), value)
        return value

    def __setitem__(self, key, value):
        self.d_store[key] = (time.time(), value)

    def __delitem__(self, key):
        del self.d_store[key]

    def cleanup(self, timeout):
        now = time.time()
        to_del = []
        for k, (atime, value) in iteritems(self.d_store):
            if now - atime > timeout:
                to_del.append(k)

        # to avoid exception on "dict change during iterations"
        for k in to_del:
            del self.d_store[k]


if __name__ == "__main__":
    import doctest

    doctest.testmod()
