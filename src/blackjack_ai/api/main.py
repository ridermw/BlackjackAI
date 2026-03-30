from __future__ import annotations

import uvicorn

from blackjack_ai.api.app import create_app
from blackjack_ai.config import get_settings

app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
