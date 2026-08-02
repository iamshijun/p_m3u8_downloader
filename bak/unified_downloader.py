#!/usr/bin/env python3
"""
Unified M3U8 Downloader - Auto-configures headers and proxy based on domain

Replaces individual wrapper scripts with intelligent domain detection
and configuration merging.

Usage:
    python3 unified_downloader.py <m3u8_url> [options...]

Domains:
    missav.ai      - Uses proxy and Referer header
    7mmtv.sx       - Uses 7mmtv.sx Referer header
    surrit.store   - Uses Referer and Origin headers
    wowstream2.cloud- Basic domain support

Examples:
    # Download from missav.ai (auto applies proxy and headers)
    python3 unified_downloader.py https://missav.ai/video.m3u8

    # Download from 7mmtv.sx (auto applies Referer header)
    python3 unified_downloader.py https://7mmtv.sx/video.m3u8 --no-merge

    # Override proxy for specific domain
    python3 unified_downloader.py https://missav.ai/video.m3u8 --proxy http://custom-proxy:8080

    # Download from unknown domain (minimal config)
    python3 unified_downloader.py https://example.com/video.m3u8

Configuration:
    Domain configuration is stored in domain_config.{json,toml}
    Edit this file to add new domains or modify existing ones.

Requirements:
    Python 3.6+
    Optional: toml library for TOML support (pip install toml)
"""

import os
import sys
import json
import argparse
import re
import shlex
from urllib.parse import urlparse
from typing import Dict, List, Optional, Any

try:
    import toml
    TOML_AVAILABLE = True
except ImportError:
    TOML_AVAILABLE = False


class ConfigManager:
    """Manages configuration loading and parsing"""

    def __init__(self, script_dir: str):
        self.script_dir = script_dir
        self.config_data = None
        self.load_config()

    def load_config(self):
        """Load configuration from TOML or JSON file"""
 # Try TOML first (if toml library is available), then JSON
        config_files = []

        if TOML_AVAILABLE:
            config_files.insert(0, os.path.join(self.script_dir, "domain_config.toml"))
        config_files.append(os.path.join(self.script_dir, "domain_config.json"))

        config_file = None
        for file_path in config_files:
            if os.path.exists(file_path):
                config_file = file_path
                break

        if not config_file:
            print(f"Error: No configuration file found in {self.script_dir}", file=sys.stderr)
            sys.exit(1)

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                if config_file.endswith('.toml') and TOML_AVAILABLE:
                    self.config_data = toml.load(f)
                else:
                    self.config_data = json.load(f)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Error loading configuration file: {e}", file=sys.stderr)
            sys.exit(1)

    def get_domain_config(self, domain: str) -> Dict[str, Any]:
        """Get configuration for a specific domain"""
        if not domain:
            return {}

        # Try exact match first with both formats
        if domain in self.config_data.get('domains', {}):
            return self.config_data['domains'][domain]

        # Try TOML format (with _)
        domain_key = domain.replace('.', '_')
        if domain_key in self.config_data.get('domains', {}):
            return self.config_data['domains'][domain_key]

        # Try partial match (e.g., missav.ai matches sub.missav.ai)
        for config_domain in self.config_data.get('domains', {}).keys():
            if domain.endswith(config_domain.replace('_', '.')):
                return self.config_data['domains'][config_domain]

        return {}

    def get_user_agent(self) -> str:
        """Get default user agent"""
        return self.config_data.get('user_agent', '')

    def get_default_proxy(self) -> str:
        """Get default proxy"""
        return self.config_data.get('default_proxy', '')


class DomainProcessor:
    """Handles domain detection and configuration"""

    @staticmethod
    def extract_domain(url: str) -> str:
        """Extract domain from URL"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc
            # Remove port
            domain = domain.split(':')[0]
            return domain
        except:
            return ""

    @staticmethod
    def parse_config_args(args_str: str) -> List[str]:
        """Parse configuration args string supporting both --arg=value and --arg value formats"""
        if not args_str.strip():
            return []

        args = []
        try:
            # First try to parse with shlex (supports quoted arguments)
            lexer = shlex.shlex(args_str, posix=True)
            lexer.whitespace_split = True
            lexer.whitespace = ' \t\n\r\f\v'

            # Special handling for --arg=value format
            tokens = []
            for token in lexer:
                if token.startswith('--') and '=' in token:
                    # Split --arg=value into --arg value
                    parts = token.split('=', 1)
                    tokens.extend(parts)
                else:
                    tokens.append(token)

            args = tokens
        except:
            # Fallback to simple split if shlex fails
            args = args_str.split()

        return args

    @staticmethod
    def build_config_args(config: Dict[str, Any], user_args: List[str]) -> List[str]:
        """Build command line arguments from configuration"""
        config_args = []

        # Add User-Agent if configured
        user_agent = config.get('user_agent')
        if user_agent:
            config_args.extend(['-H', f'User-Agent: {user_agent}'])

        # Add headers from domain config
        headers = config.get('headers', {})
        for key, value in headers.items():
            config_args.extend(['-H', f'{key}: {value}'])

        # Add proxy if configured and not empty string
        proxy = config.get('proxy')
        if proxy and proxy.strip():
            config_args.extend(['--proxy', proxy])

        # Add extra args from domain config (e.g., " -r 7 -t 25" or "--workers=5")
        args = config.get('args', '').strip()
        if args:
            config_args.extend(DomainProcessor.parse_config_args(args))

        return config_args


def create_parser():
    """Create argument parser for unified downloader"""
    parser = argparse.ArgumentParser(
        description="Unified M3U8 Downloader - Auto-configures headers and proxy based on domain",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Positional argument - M3U8 URL or file path
    parser.add_argument("m3u8_input", help="M3U8 file path or URL")
    parser.add_argument("--upload", "-u", default=None, metavar="REMOTE_DIR",
                        help="上传到百度云盘, 后跟远程目录路径, 如 /apps/movies/my_video/")
    # Options
    parser.add_argument("--config", action="store_true",
                       help="Show current domain configuration")
    return parser


def main():
    # Get script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Load configuration
    config_manager = ConfigManager(script_dir)

    # Create argument parser
    parser = create_parser()
    args = parser.parse_args()
    if args.config : 
        show_config(config_manager)
        sys.exit(0)

    # Extract M3U8 URL from arguments
    m3u8_url = args.m3u8_input
    if m3u8_url.startswith('http'):
        domain = DomainProcessor.extract_domain(m3u8_url)
        print(f"Detected domain: {domain}", file=sys.stderr)
    else:
        domain = ""
        print("Info: Local file detected, using generic configuration", file=sys.stderr)

    # Get domain configuration
    domain_config = config_manager.get_domain_config(domain)
    if "user_agent" not in domain_config or not domain_config["user_agent"]:
        # Use default user agent if not set in domain config
        domain_config["user_agent"] = config_manager.get_user_agent()
 
    config_args = DomainProcessor.build_config_args(domain_config, sys.argv[1:])

    # Prepare all arguments for the original downloader
    downloader_path = os.path.join(script_dir, 'm3u8_downloader.py')

    if args.upload:
        config_args.extend(['--upload', args.upload])
    # Execute the original downloader with all arguments
    all_args = ['python3', downloader_path, m3u8_url] + config_args
    os.execv(sys.executable, all_args)

def show_help():
    """Display help information"""
    help_text = __doc__
    print(help_text)


def show_config(config_manager: ConfigManager):
    """Show current configuration"""
    print("Current domain configuration:")
    domains = config_manager.config_data.get('domains', {})
    for domain, settings in domains.items():
        proxy = settings.get('proxy', 'none')
        headers = list(settings.get('headers', {}).keys())
        print(f"  {domain}: proxy={proxy}, headers={headers}")

    if TOML_AVAILABLE:
        print(f"\nConfiguration format: TOML support available")
    else:
        print(f"\nConfiguration format: TOML support not available (install 'toml' package)")


if __name__ == '__main__':
    main()