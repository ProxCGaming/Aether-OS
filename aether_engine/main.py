import uvicorn, logging
from aether_engine.app import app

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s")


def start_engine(host: str = "127.0.0.1", port: int = 8000):
    if host != "127.0.0.1":
        raise ValueError(f"Engine must bind to 127.0.0.1, got {host}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    start_engine()
