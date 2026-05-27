import os

def test_path_exists():
    path = "/Users/denisetijerina/Documents/LCC-WFM"
    assert os.path.exists(path), f"Path does not exist: {path}"

def test_path_is_directory():
    path = "/Users/denisetijerina/Documents/LCC-WFM"
    assert os.path.isdir(path), f"Path is not a directory: {path}"

def test_path_is_accessible():
    path = "/Users/denisetijerina/Documents/LCC-WFM"
    assert os.access(path, os.R_OK), f"Path is not readable: {path}"

def test_path_is_writable():
    path = "/Users/denisetijerina/Documents/LCC-WFM"
    assert os.access(path, os.W_OK), f"Path is not writable: {path}"
