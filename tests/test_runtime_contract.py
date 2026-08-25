import unittest

import production_runtime_v2


class RuntimeContractTests(unittest.TestCase):
    def test_view_functions_resolves_flask_registry_from_app_module(self):
        class FlaskLike:
            def __init__(self):
                self.view_functions = {"handle_messages": object()}

        class AppModuleLike:
            def __init__(self):
                self.app = FlaskLike()

        app_module = AppModuleLike()
        self.assertIs(
            production_runtime_v2._view_functions_for(app_module),
            app_module.app.view_functions,
        )


if __name__ == "__main__":
    unittest.main()
