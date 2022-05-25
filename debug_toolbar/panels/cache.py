import functools
import time

from asgiref.local import Local
from django.conf import settings
from django.core.cache import CacheHandler, caches
from django.utils.translation import gettext_lazy as _, ngettext

from debug_toolbar.panels import Panel
from debug_toolbar.utils import get_stack_trace, get_template_info, render_stacktrace

# The order of the methods in this list determines the order in which they are listed in
# the Commands table in the panel content.
WRAPPED_CACHE_METHODS = [
    "add",
    "get",
    "set",
    "get_or_set",
    "touch",
    "delete",
    "clear",
    "get_many",
    "set_many",
    "delete_many",
    "has_key",
    "incr",
    "decr",
    "incr_version",
    "decr_version",
]


class CachePanel(Panel):
    """
    Panel that displays the cache statistics.
    """

    template = "debug_toolbar/panels/cache.html"

    _context_locals = Local()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.total_time = 0
        self.hits = 0
        self.misses = 0
        self.calls = []
        self.counts = {name: 0 for name in WRAPPED_CACHE_METHODS}

    @classmethod
    def current_instance(cls):
        """
        Return the currently enabled CachePanel instance or None.

        If a request is in process with a CachePanel enabled, this will return that
        panel (based on the current thread or async task).  Otherwise it will return
        None.
        """
        return getattr(cls._context_locals, "current_instance", None)

    @classmethod
    def ready(cls):
        if not hasattr(CacheHandler, "_djdt_patched"):
            # Wrap the CacheHander.create_connection() method to monkey patch any new
            # cache connections that are opened while instrumentation is enabled.  In
            # the interests of thread safety, this is done once at startup time and
            # never removed.
            original_method = CacheHandler.create_connection

            @functools.wraps(original_method)
            def wrapper(self, alias):
                cache = original_method(self, alias)
                panel = cls.current_instance()
                if panel is not None:
                    panel._monkey_patch_cache(cache)
                return cache

            CacheHandler.create_connection = wrapper
            CacheHandler._djdt_patched = True

    def _store_call_info(
        self,
        name,
        time_taken,
        return_value,
        args,
        kwargs,
        trace,
        template_info,
        backend,
    ):
        if name == "get" or name == "get_or_set":
            if return_value is None:
                self.misses += 1
            else:
                self.hits += 1
        elif name == "get_many":
            if "keys" in kwargs:
                keys = kwargs["keys"]
            else:
                keys = args[0]
            self.hits += len(return_value)
            self.misses += len(keys) - len(return_value)
        time_taken *= 1000

        self.total_time += time_taken
        self.counts[name] += 1
        self.calls.append(
            {
                "time": time_taken,
                "name": name,
                "args": args,
                "kwargs": kwargs,
                "trace": render_stacktrace(trace),
                "template_info": template_info,
                "backend": backend,
            }
        )

    def _record_call(self, cache, name, original_method, args, kwargs):
        # Some cache backends implement certain cache methods in terms of other cache
        # methods (e.g. get_or_set() in terms of get() and add()).  In order to only
        # record the calls made directly by the user code, set the _djdt_recording flag
        # here to cause the monkey patched cache methods to skip recording additional
        # calls made during the course of this call.
        cache._djdt_recording = True
        t = time.time()
        value = original_method(*args, **kwargs)
        t = time.time() - t
        cache._djdt_recording = False

        self._store_call_info(
            name=name,
            time_taken=t,
            return_value=value,
            args=args,
            kwargs=kwargs,
            trace=get_stack_trace(),
            template_info=get_template_info(),
            backend=cache,
        )
        return value

    def _monkey_patch_method(self, cache, name):
        original_method = getattr(cache, name)

        @functools.wraps(original_method)
        def wrapper(*args, **kwargs):
            # If this call is being made as part of the implementation of another cache
            # method, don't record it.
            if cache._djdt_recording:
                return original_method(*args, **kwargs)
            else:
                return self._record_call(cache, name, original_method, args, kwargs)

        wrapper._djdt_wrapped = original_method
        setattr(cache, name, wrapper)

    def _monkey_patch_cache(self, cache):
        if not hasattr(cache, "_djdt_patched"):
            for name in WRAPPED_CACHE_METHODS:
                self._monkey_patch_method(cache, name)
            cache._djdt_patched = True
            cache._djdt_recording = False

    @staticmethod
    def _unmonkey_patch_cache(cache):
        if hasattr(cache, "_djdt_patched"):
            for name in WRAPPED_CACHE_METHODS:
                original_method = getattr(cache, name)._djdt_wrapped
                if original_method.__func__ == getattr(cache.__class__, name):
                    delattr(cache, name)
                else:
                    setattr(cache, name, original_method)
            del cache._djdt_patched
            del cache._djdt_recording

    # Implement the Panel API

    nav_title = _("Cache")

    @property
    def nav_subtitle(self):
        cache_calls = len(self.calls)
        return ngettext(
            "%(cache_calls)d call in %(time).2fms",
            "%(cache_calls)d calls in %(time).2fms",
            cache_calls,
        ) % {"cache_calls": cache_calls, "time": self.total_time}

    @property
    def title(self):
        count = len(getattr(settings, "CACHES", ["default"]))
        return ngettext(
            "Cache calls from %(count)d backend",
            "Cache calls from %(count)d backends",
            count,
        ) % {"count": count}

    def enable_instrumentation(self):
        # Monkey patch all open cache connections.  Django maintains cache connections
        # on a per-thread/async task basis, so this will not affect any concurrent
        # requests.  The monkey patch of CacheHander.create_connection() installed in
        # the .ready() method will ensure that any new cache connections that get opened
        # during this request will also be monkey patched.
        for cache in caches.all(initialized_only=True):
            self._monkey_patch_cache(cache)
        # Mark this panel instance as the current one for the active thread/async task
        # context.  This will be used by the CacheHander.create_connection() monkey
        # patch.
        self._context_locals.current_instance = self

    def disable_instrumentation(self):
        if hasattr(self._context_locals, "current_instance"):
            del self._context_locals.current_instance
        for cache in caches.all(initialized_only=True):
            self._unmonkey_patch_cache(cache)

    def generate_stats(self, request, response):
        self.record_stats(
            {
                "total_calls": len(self.calls),
                "calls": self.calls,
                "total_time": self.total_time,
                "hits": self.hits,
                "misses": self.misses,
                "counts": self.counts,
            }
        )

    def generate_server_timing(self, request, response):
        stats = self.get_stats()
        value = stats.get("total_time", 0)
        title = "Cache {} Calls".format(stats.get("total_calls", 0))
        self.record_server_timing("total_time", title, value)
