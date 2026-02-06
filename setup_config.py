#!/usr/bin/env python3
import logging
import sys
import os

# Ensure the current directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from orchestrator.utils.cli import setup_orchestrator, show_settings

def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    print("Lobs Orchestrator Setup")
    print("-----------------------")
    
    args = setup_orchestrator()
    
    print("\nSettings updated successfully.")
    show_settings()
    print("You can now run the orchestrator with: python3 main.py")

if __name__ == "__main__":
    main()

