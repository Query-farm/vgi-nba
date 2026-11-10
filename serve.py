# /// script
# requires-python = ">=3.13"
# dependencies = ["vgi-python[http]>=0.50.0,<0.51", "httpx>=0.27,<1", "pyarrow>=18,<26"]
# ///
"""HTTP entry point for the NBA-family VGI worker."""

from vgi_nba.worker import main_http

if __name__ == "__main__":
    main_http()
