#!/bin/bash

python3 -m venv py
source py/bin/activate
python3 -m ensurepip
python3 -m pip install -r requirements.txt