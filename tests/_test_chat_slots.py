"""聊天并发上限的自动扩容契约。

``WB_MAX_CONCURRENT_CHAT=auto`` 时按可用账号数扩容，只增不减；设为数字时保持固定上限。
运行方式：python tests/_test_chat_slots.py，不需要上游凭据，也不访问网络。
"""
import os
import sys
import tempfile
import threading
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="chat-slots-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


class ChatSlotResizeTests(unittest.TestCase):
    """resize_chat_slots 只提高上限，绝不缩小。"""

    def setUp(self):
        self._auto = wb_proxy.CHAT_SLOTS_AUTO
        self._max = wb_proxy.MAX_CONCURRENT_CHAT
        self._slots = wb_proxy._chat_slots

    def tearDown(self):
        wb_proxy.CHAT_SLOTS_AUTO = self._auto
        wb_proxy.MAX_CONCURRENT_CHAT = self._max
        wb_proxy._chat_slots = self._slots

    def test_auto_sizes_from_the_pool(self):
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        self.assertEqual(wb_proxy.resize_chat_slots(110), 110)
        self.assertEqual(wb_proxy.MAX_CONCURRENT_CHAT, 110)
        # 新信号量确实带有这么多许可。
        acquired = 0
        while wb_proxy._chat_slots.acquire(blocking=False):
            acquired += 1
        self.assertEqual(acquired, 110)
        for _ in range(acquired):
            wb_proxy._chat_slots.release()

    def test_small_pool_falls_back_to_the_floor(self):
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        self.assertEqual(wb_proxy.resize_chat_slots(3), wb_proxy.CHAT_SLOTS_FLOOR)

    def test_never_shrinks(self):
        """缩小上限会让 release 次数超过上限，触发 ValueError。"""
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        wb_proxy.resize_chat_slots(200)
        slots = wb_proxy._chat_slots
        self.assertEqual(wb_proxy.resize_chat_slots(5), 200)
        self.assertIs(wb_proxy._chat_slots, slots)

    def test_fixed_setting_is_a_no_op(self):
        """WB_MAX_CONCURRENT_CHAT 设为数字时保持固定上限。"""
        wb_proxy.CHAT_SLOTS_AUTO = False
        wb_proxy.MAX_CONCURRENT_CHAT = 32
        slots = wb_proxy._chat_slots
        self.assertEqual(wb_proxy.resize_chat_slots(500), 32)
        self.assertEqual(wb_proxy.MAX_CONCURRENT_CHAT, 32)
        self.assertIs(wb_proxy._chat_slots, slots)

    def test_garbage_input_is_ignored(self):
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        self.assertEqual(wb_proxy.resize_chat_slots(None), wb_proxy.CHAT_SLOTS_FLOOR)
        self.assertEqual(wb_proxy.resize_chat_slots("abc"), wb_proxy.CHAT_SLOTS_FLOOR)


class SlotAccountingTests(unittest.TestCase):
    """扩容不能破坏在途计数。"""

    def test_resize_preserves_held_slots(self):
        saved_auto = wb_proxy.CHAT_SLOTS_AUTO
        saved_max = wb_proxy.MAX_CONCURRENT_CHAT
        saved_slots = wb_proxy._chat_slots
        try:
            wb_proxy.CHAT_SLOTS_AUTO = True
            wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
            wb_proxy._chat_slots = threading.BoundedSemaphore(wb_proxy.CHAT_SLOTS_FLOOR)
            self.assertTrue(wb_proxy._chat_slots.acquire(blocking=False))
            wb_proxy.resize_chat_slots(64)
            # 扩容前持有的许可属于旧信号量，在新信号量上释放不能触发异常。
            for _ in range(64):
                self.assertTrue(wb_proxy._chat_slots.acquire(blocking=False))
            for _ in range(64):
                wb_proxy._chat_slots.release()
        finally:
            wb_proxy.CHAT_SLOTS_AUTO = saved_auto
            wb_proxy.MAX_CONCURRENT_CHAT = saved_max
            wb_proxy._chat_slots = saved_slots


if __name__ == "__main__":
    unittest.main(verbosity=2)
