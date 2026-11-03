"""Disposable review-stop acceptance fixture. Do not merge this test MR."""
from pathlib import Path
import json


class ManifestReader:
    def __init__(self, root):
        self.root = Path(root)
        self.cache = {}

    def read(self, relative):
        path = self.root / relative
        if not str(path).startswith(str(self.root)):
            raise ValueError("outside root")
        key = str(path)
        if key not in self.cache:
            self.cache[key] = path.read_text(encoding="utf-8")
        return self.cache[key]

    def records(self, manifest):
        values = json.loads(self.read(manifest))
        output = []
        for value in values:
            if value.get("enabled", True):
                output.append({"name": value["name"], "body": self.read(value["path"])})
        return output


def summarize(records):
    sizes = [len(record["body"]) for record in records]
    return {"count": len(sizes), "mean": sum(sizes) / len(sizes), "largest": max(sizes)}


def read_batches(root, manifests):
    reader = ManifestReader(root)
    results = []
    for manifest in manifests:
        try:
            records = reader.records(manifest)
            results.append({"manifest": manifest, "summary": summarize(records)})
        except (OSError, ValueError):
            continue
    return results
