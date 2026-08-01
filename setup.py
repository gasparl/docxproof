"""Compatibility fallback for installers without PEP 660 editable support."""

from setuptools import find_packages, setup

setup(
    name="docxproof",
    version="5.0.0",
    description="Formatting-preserving AI proofreading for DOCX files",
    packages=find_packages(include=("docxproof", "docxproof.*")),
    python_requires=">=3.10",
    install_requires=[
        "lxml>=5.3,<7",
        "openai>=2.0,<3",
        "pydantic>=2.10,<3",
    ],
    extras_require={
        "test": [
            "python-docx>=1.1,<2",
            "Pillow>=10,<13",
        ]
    },
    entry_points={"console_scripts": ["docx-proofread=docxproof.cli:cli_main"]},
)
