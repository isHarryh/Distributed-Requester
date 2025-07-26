Distributed-Requester
==========
Distributed stress testing tool for web servers  
分布式网络服务器压力测试工具

## Introduction

This tool is designed for distributed stress testing of web servers. It supports both offline mode with predefined tasks and distributed mode where clients will fetch tasks from server in advance.

## Get Started

### Installation

1. Python >=3.9 is required.
2. Use poetry (recommended) or pip to install dependencies.

### Usage

To use the tool in command line:

```txt
usage: main.py [-h] [-s] [-c] [config_file]

positional arguments:
  config_file   Configuration file path (default: config.json)

options:
  -h, --help    show this help message and exit
  -s, --server  Run in server mode
  -c, --client  Run in client mode
```

If no argument is provided, it will run in `--client` mode with `config.json` by default.

If config file is provided but no option is provided, it will run in offline mode.

### Build

To build an executable file:

Install poetry (a package manager), then:

```bash
poetry env use python
poetry install
poetry run python Build.py
```

> Note: Change the word "python" to the actual python interpreter you installed (like "python3" or "python3.12") if necessary.

## Licensing

This project is licensed under the MIT License. See the [License](https://github.com/isHarryh/Distributed-Requester/blob/main/LICENSE) file for more details.
