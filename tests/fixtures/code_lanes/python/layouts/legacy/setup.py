from setuptools import find_packages, setup

setup(
    name="legacy",
    package_dir={"": "python"},
    packages=find_packages(where="python"),
    entry_points={
        "console_scripts": [
            "legacy-tool = legacy.tool:main",
        ],
    },
)
