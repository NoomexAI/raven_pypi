"""Create an example knowledge. Run this before ingest_knowledge.py.

Install noomexai-raven==0.2.1, then run from the repository root:
    python examples/create_knowledge.py
"""

import argparse
import asyncio
from pathlib import Path

from nraven import Raven
from nraven.core.errors import ErrorCode, RavenError


async def main(home: Path, name: str) -> None:
    raven = Raven(home)
    await raven.start()
    try:
        task = await raven.create_knowledge(name, "Engineering reference documents.")
        try:
            knowledge = await task.result()
        except RavenError as error:
            if error.code != ErrorCode.KNOWLEDGE_ALREADY_EXISTS:
                raise
            print(f"Knowledge {name!r} already exists.")
        else:
            print(f"Created {knowledge.name!r} in {knowledge.dir_path}")

        for item in await raven.list_knowledges():
            print(f"  {item['name']}: {item['user_summary']}")
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path("raven-home/examples"))
    parser.add_argument("--knowledge", default="engineering")
    args = parser.parse_args()
    asyncio.run(main(args.home, args.knowledge))
