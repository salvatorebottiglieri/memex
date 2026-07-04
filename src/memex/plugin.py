"""Utilities for plugin-style class loading from 'module:Class' strings.

Used by load_fetcher, load_agent, load_telegram_source, and any
future test-injectable seam.
"""


def load_class(module_path: str) -> object:
    """Import and instantiate a class from a ``module:ClassName`` string.

    Args:
        module_path: ``module:Class`` import string (e.g. ``my.module:MyClass``).

    Returns:
        An instance of the requested class.

    Raises:
        ImportError: If the module or class cannot be found.
    """
    module_name, _, class_name = module_path.partition(":")
    import importlib

    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_name)
    return cls()
