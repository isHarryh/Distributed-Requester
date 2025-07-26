import asyncio
import sys
import argparse

import httpx

from src.Config import load_config, TaskConfig, ConfigError, Config, COMPATIBLE_VERSIONS
from src.Client import Client
from src.Server import Server


class TaskDistributionError(Exception):
    """Custom exception for task distribution errors"""


async def fetch_task_from_server(server_url: str) -> TaskConfig:
    """Fetch task configuration from distributed server"""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{server_url}/get_config", timeout=10.0)
            response.raise_for_status()

            result = response.json()
            if result["code"] != 0:
                raise TaskDistributionError(f"Server error: {result['msg']}")

            config_data = result["data"]
            if not config_data["tasks"]:
                raise TaskDistributionError("No task received from server")
            if not config_data["version"]:
                raise TaskDistributionError("Remote config version field is missing")
            if not config_data["version"] in COMPATIBLE_VERSIONS:
                raise TaskDistributionError(
                    f"Incompatible remote config version: {config_data['version']}, "
                    + f"expected one of {COMPATIBLE_VERSIONS}"
                )

            # Return the first (and only) task from server response
            return TaskConfig(**config_data["tasks"][0])

        except TaskDistributionError as e:
            raise e
        except Exception as e:
            raise TaskDistributionError(f"Failed to connect to server: {type(e).__name__} - {e}")


async def run_offline_mode(config: Config):
    """Run offline stress test mode"""

    async def run_task(task_config: TaskConfig):
        """Run a single stress test task"""
        print(f"\nStarting task: {task_config.name}")

        client = Client(task_config, config)
        await client.run()

    print("Running in offline mode")
    print("=" * 50)

    # Run all tasks
    for _, task in enumerate(config.tasks):
        try:
            await run_task(task)
        except KeyboardInterrupt:
            print(f"\nTask '{task.name}' interrupted")
            break
        except Exception as e:
            print(f"\nTask '{task.name}' failed: {e}")
            continue

    print("\nAll tasks completed")


async def run_client_mode(config: Config):
    """Run distributed client mode"""

    print("Running in client mode")
    print("=" * 50)

    if not config.client:
        print("Error: Client configuration is required for distributed mode")
        sys.exit(1)

    while True:
        print(f"Connecting to server...")

        try:
            # Fetch task from server
            task_config = await fetch_task_from_server(config.client.server_url)
            print(f"Received task: {task_config.name}")

            # Create and run client with server reporting enabled
            client = Client(task_config, config)
            await client.run()

        except TaskDistributionError as e:
            print(f"Cannot get new task: {e}")
        except Exception as e:
            print(f"Error: Something went wrong during task execution: {e}")
            sys.exit(1)

        print("\nWaiting for 30s to reconnect to server...")
        await asyncio.sleep(30)


async def run_server_mode(config: Config):
    """Run server mode"""

    print("Running in server mode")
    print("=" * 50)

    if not config.server:
        print("Error: Server configuration is required to run server mode")
        sys.exit(1)

    try:
        print(f"Starting server on port {config.server.port}")
        server = Server(config)
        await server.serve()

    except KeyboardInterrupt:
        print("\nServer stopped")
    except Exception as e:
        print(f"Error: Server failed to start: {e}")
        sys.exit(1)


async def main_async():
    """Async main function"""
    parser = argparse.ArgumentParser(description="Distributed Requester")
    parser.add_argument("config_file", help="Configuration file path")
    parser.add_argument("-s", "--server", action="store_true", help="Run in server mode")
    parser.add_argument("-c", "--client", action="store_true", help="Run in client mode")

    args = parser.parse_args()

    # Validate argument combinations
    if args.server and args.client:
        print("Error: Cannot specify both --server and --client options")
        sys.exit(1)

    # Load configuration file
    try:
        config = load_config(args.config_file)
    except ConfigError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    # Determine run mode
    if args.server:
        await run_server_mode(config)
    elif args.client:
        await run_client_mode(config)
    else:
        await run_offline_mode(config)


def main():
    """Main function entry point"""
    try:
        if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nProgram interrupted")
    except Exception as e:
        print(f"\nProgram execution failed: {e}")


if __name__ == "__main__":
    main()
