import unittest

import bot
from apps.feishu import ai, app, router, transport, views


class ArchitectureTest(unittest.TestCase):
    def test_compatibility_entrypoint_reexports_public_handlers(self):
        self.assertIs(bot.main, app.main)
        self.assertIs(bot.handle_command, router.handle_command)
        self.assertIs(bot.send_text, transport.send_text)
        self.assertIs(bot.render_status, views.render_status)
        self.assertIs(bot.render_todo, views.render_todo)
        self.assertIs(bot.render_memory, views.render_memory)
        self.assertIs(bot.render_notify, views.render_notify)
        self.assertIs(bot.render_page, views.render_page)
        self.assertIn("add_todo", ai.TOOL_HANDLERS)
        self.assertTrue(callable(ai.weather_reply))


if __name__ == "__main__":
    unittest.main()
