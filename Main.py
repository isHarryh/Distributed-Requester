import asyncio
import sys
import argparse

import httpx

from src.Config import load_config, TaskConfig, ConfigError, Config, COMPATIBLE_VERSIONS
from src.Client import Client
from src.Server import Server
from src.utils.Logger import Logger


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

    Logger.info("Running in offline mode")

    # Run all tasks
    for _, task in enumerate(config.tasks):
        try:
            Logger.info(f"Starting task: {task.name}")
            await Client(task, config).run()
        except KeyboardInterrupt:
            Logger.error(f"Task '{task.name}' interrupted")
            break
        except Exception as e:
            Logger.error(f"Task '{task.name}' failed: {type(e).__name__} {e}")
            continue

    Logger.info("\nAll tasks completed")


async def run_client_mode(config: Config):
    """Run distributed client mode"""

    Logger.info("Running in client mode")

    if not config.client:
        Logger.error("Client configuration is required to run client mode")
        sys.exit(1)

    while True:
        Logger.info(f"Connecting to server...")

        try:
            # Fetch task from server
            Logger.info(f"Connecting to server at {config.client.server_url} to fetch task")
            task_config = await fetch_task_from_server(config.client.server_url)
            Logger.info(f"Received task: {task_config.name}")

            # Create and run client with server reporting enabled
            client = Client(task_config, config)
            await client.run()

        except TaskDistributionError as e:
            Logger.error(f"Cannot get new task: {e}")
        except Exception as e:
            Logger.error(f"Error fetching task due to unexpected exception: {type(e).__name__} {e}")
            sys.exit(1)

        Logger.info("\nWaiting for 30s to reconnect to server...")
        await asyncio.sleep(30)


async def run_server_mode(config_path: str):
    """Run server mode"""

    Logger.info("Running in server mode")

    # Load config to check server configuration
    try:
        config = load_config(config_path)
    except ConfigError as e:
        Logger.error(f"Configuration error: {e}")
        sys.exit(1)

    if not config.server:
        Logger.error("Server configuration is required to run server mode")
        sys.exit(1)

    try:
        Logger.info(f"Starting server on port {config.server.port}")
        server = Server(config_path)
        await server.serve()

    except KeyboardInterrupt:
        Logger.info("Server stopped due to interruption")
    except Exception as e:
        Logger.error(f"Server failed due to unexpected exception: {type(e).__name__} {e}")
        sys.exit(1)


async def main_async():
    """Async main function"""

    parser = argparse.ArgumentParser(description="Distributed Requester")
    parser.add_argument(
        "config_file", nargs="?", default="config.json", help="Configuration file path (default: config.json)"
    )
    parser.add_argument("-s", "--server", action="store_true", help="Run in server mode")
    parser.add_argument("-c", "--client", action="store_true", help="Run in client mode")

    # If no arguments, default to client mode with config.json
    if len(sys.argv) == 1:
        Logger.debug("No arguments provided, defaulting to client mode with config.json")
        args = parser.parse_args(["config.json", "-c"])
    else:
        Logger.debug("Parsing command line arguments")
        args = parser.parse_args()

    # Validate argument combinations
    if args.server and args.client:
        Logger.error("Cannot specify both --server and --client options")
        sys.exit(1)

    # Load configuration file
    try:
        Logger.info(f"Loading configuration from: {args.config_file}")
        config = load_config(args.config_file)
    except ConfigError as e:
        Logger.error(f"Configuration error: {e}")
        sys.exit(1)

    # Determine run mode
    if args.server:
        await run_server_mode(args.config_file)
    elif args.client:
        await run_client_mode(config)
    else:
        await run_offline_mode(config)

    Logger.info("Program completed successfully")


def main():
    """Main function entry point"""
    Logger.reset()
    Logger.enable_console()
    Logger.enable_file()

    try:
        if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        asyncio.run(main_async())
    except KeyboardInterrupt:
        Logger.error("Program interrupted")
    except Exception as e:
        Logger.error(f"Program execution failed: {type(e).__name__} {e}")


if __name__ == "__main__":
    main()
