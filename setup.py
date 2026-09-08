from setuptools import setup, find_packages

setup(
    name="research-paper-pdf-downloader",
    version="1.0.0",
    description="Automated pipeline for downloading academic paper PDFs via Semantic Scholar metadata and multi-provider open-access resolution.",
    python_requires=">=3.10",
    packages=find_packages(),
    py_modules=["paper_data", "main"],
    package_data={
        "": ["config.json"],
    },
    install_requires=[
        "requests>=2.31.0",
        "python-dotenv>=1.0.0",
        "pypdf>=4.0",
    ],
    entry_points={
        "console_scripts": [
            "paper-downloader=main:main",
        ],
    },
)