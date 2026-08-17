from setuptools import Extension, setup
from Cython.Build import cythonize

modules = [
    Extension("foreground_window_capture", ["foreground_window_capture.py"]),
    Extension("foreground_window_service", ["foreground_window_service.py"]),
]

setup(
    ext_modules=cythonize(
        modules,
        compiler_directives={"language_level": "3"},
    )
)