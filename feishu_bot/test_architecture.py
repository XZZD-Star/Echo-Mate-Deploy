import unittest

import bot
from apps.feishu import app, messaging, router
from apps.feishu.features import tools, weather
from apps.feishu.views import memory, notify, page, status, todo


class ArchitectureTest(unittest.TestCase):
    def test_compatibility_entrypoint_reexports_public_handlers(self):
        self.assertIs(bot.main, app.main)
        self.assertIs(bot.handle_command, router.handle_command)
        self.assertIs(bot.send_text, messaging.send_text)
        self.assertIs(bot.render_status, status.render_status)
        self.assertIs(bot.render_todo, todo.render_todo)
        self.assertIs(bot.render_memory, memory.render_memory)
        self.assertIs(bot.render_notify, notify.render_notify)
        self.assertIs(bot.render_page, page.render_page)
        self.assertIn("add_todo", tools.TOOL_HANDLERS)
        self.assertTrue(callable(weather.weather_reply))


if __name__ == "__main__":
    unittest.main()
