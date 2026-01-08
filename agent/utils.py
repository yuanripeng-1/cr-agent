import re
import sys
import os
from typing import List

def parse_diff_file_paths(diff_content: str) -> List[str]:
    """
    Parses git diff content to extract changed file paths.
    """
    file_paths = set()
    # Look for lines starting with 'diff --git a/path b/path'
    # or '--- a/path' '+++ b/path'
    
    # Regex for '+++ b/path/to/file'
    # We generally care about the 'b' side (new version)
    pattern = re.compile(r'^\+\+\+ b/(.+)$', re.MULTILINE)
    
    matches = pattern.findall(diff_content)
    for match in matches:
        # Filter out /dev/null for deleted files if necessary
        if match != '/dev/null':
            file_paths.add(match.strip())
            
    return list(file_paths)

class Tee(object):
    def __init__(self, *files):
        self.files = files
    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()
    def flush(self):
        for f in self.files:
            f.flush()

def setup_log_redirection(log_path: str):
    """
    Redirects stdout and stderr to both console and a log file.
    """
    log_file = open(log_path, 'a', encoding='utf-8')
    sys.stdout = Tee(sys.stdout, log_file)
    sys.stderr = Tee(sys.stderr, log_file)
    return log_file

