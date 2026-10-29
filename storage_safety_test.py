import copy
import tempfile
import unittest
from pathlib import Path

from storage_safety import install_storage_guards, is_ai_protected_path, filter_storage_status


class FakeStorageAPI:
    def __init__(self):
        self.state = {
            "running": False,
            "scanned": 4,
            "items": [
                {"id": "temp", "path": r"C:\Users\LxUltra\AppData\Local\Temp\old.tmp", "size": 10},
                {"id": "hf", "path": r"C:\Users\LxUltra\.cache\huggingface\hub\model.gguf", "size": 100},
                {"id": "hf-cache", "path": r"C:\Users\LxUltra\.cache\huggingface\hub\other.safetensors", "size": 50},
                {"id": "ai-project", "path": r"D:\AI Projects\vision\cache\old.bin", "size": 200},
            ],
        }
        self.cleanup_calls = []

    def storage_scan_status(self):
        return copy.deepcopy(self.state)

    def storage_cleanup(self, identifiers):
        self.cleanup_calls.append(list(identifiers))
        return {"ok": True, "moved": len(identifiers)}


class StorageSafetyTests(unittest.TestCase):
    def test_protects_known_ai_apps_models_and_projects(self):
        for path in (
            r"C:\Users\LxUltra\.ollama\models\llama.gguf",
            r"D:\ComfyUI_windows_portable\ComfyUI\models\checkpoints\checkpoint.safetensors",
            r"C:\Users\LxUltra\AppData\Local\OpenAI\ChatGPT\Cache\item.tmp",
            r"D:\AI Projects\vision\cache\old.bin",
            r"D:\LM Studio\models\model.gguf",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_ai_protected_path(path))
        self.assertFalse(is_ai_protected_path(r"C:\Users\LxUltra\AppData\Local\Temp\old.tmp"))

    def test_scan_returns_ai_candidates_in_a_separate_selectable_bucket(self):
        state = FakeStorageAPI().state
        original = copy.deepcopy(state)
        result = filter_storage_status(state)
        self.assertEqual([item["id"] for item in result["items"]], ["temp"])
        self.assertEqual([item["id"] for item in result["ai_detected_items"]], ["hf", "hf-cache", "ai-project"])
        self.assertTrue(all(item["category"] == "ai" for item in result["ai_detected_items"]))
        self.assertEqual(result["ai_detected_count"], 3)
        self.assertEqual(result["ai_detected_bytes"], 350)
        self.assertEqual(result["ai_protected_count"], 0)
        self.assertEqual(state, original)

    def test_scan_lists_auto_detected_ai_directories_and_aggregates_selectable_files(self):
        result = filter_storage_status(FakeStorageAPI().state)
        directories = {item["path"]: item for item in result["ai_detected_directories"]}
        self.assertEqual(set(directories), {
            r"C:\Users\LxUltra\.cache\huggingface",
            r"D:\AI Projects",
        })
        self.assertEqual(directories[r"C:\Users\LxUltra\.cache\huggingface"], {
            "path": r"C:\Users\LxUltra\.cache\huggingface",
            "count": 2,
            "bytes": 150,
            "source": "automatic",
        })
        self.assertEqual(directories[r"D:\AI Projects"]["count"], 1)
        self.assertEqual(directories[r"D:\AI Projects"]["source"], "automatic")

    def test_custom_protected_directory_is_reported_separately(self):
        custom = r"D:\AI Projects"
        result = filter_storage_status(FakeStorageAPI().state, [custom])
        self.assertEqual([item["id"] for item in result["ai_detected_items"]], ["hf", "hf-cache"])
        self.assertEqual(result["ai_protected_count"], 1)
        directories = {item["path"]: item for item in result["ai_protected_directories"]}
        self.assertEqual(directories[custom]["source"], "custom")

    def test_storage_api_revalidates_ids_before_cleanup(self):
        api = FakeStorageAPI()
        install_storage_guards(FakeStorageAPI)
        visible = api.storage_scan_status()
        self.assertEqual([item["id"] for item in visible["items"]], ["temp"])
        self.assertEqual([item["id"] for item in visible["ai_detected_items"]], ["hf", "hf-cache", "ai-project"])
        self.assertFalse(api.storage_cleanup(["hf"])["ok"])
        self.assertFalse(api.storage_cleanup(["not-in-current-scan"])["ok"])
        self.assertEqual(api.cleanup_calls, [])
        self.assertTrue(api.storage_cleanup(["hf"], allow_ai=True)["ok"])
        self.assertEqual(api.cleanup_calls, [["hf"]])
        self.assertTrue(api.storage_cleanup(["temp"])["ok"])
        self.assertEqual(api.cleanup_calls, [["hf"], ["temp"]])

    def test_user_selected_ai_project_roots_persist_and_block_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / "config" / "ai_exclusions.json"
            project = Path(temp) / "AI Workspace"
            project.mkdir()

            class API:
                def __init__(self):
                    self.state = copy.deepcopy(FakeStorageAPI().state)
                    self.state["items"].append({"id": "custom-ai", "path": str(project / "cache.tmp"), "size": 50})
                    self.cleanup_calls = []

                def storage_scan_status(self):
                    return copy.deepcopy(self.state)

                def storage_cleanup(self, identifiers):
                    self.cleanup_calls.append(list(identifiers))
                    return {"ok": True, "moved": len(identifiers)}

            install_storage_guards(API, store_path=store)
            api = API()
            added = api.storage_ai_exclusions_add(str(project))
            self.assertTrue(added["ok"], added)
            visible = api.storage_scan_status()
            self.assertNotIn("custom-ai", [item["id"] for item in visible["ai_detected_items"]])
            self.assertFalse(api.storage_cleanup(["custom-ai"], allow_ai=True)["ok"])
            self.assertEqual(api.cleanup_calls, [])
            listed = api.storage_ai_exclusions_list()
            self.assertEqual(listed["paths"], [str(project)])

            removed = api.storage_ai_exclusions_remove(str(project))
            self.assertTrue(removed["ok"], removed)
            self.assertIn("custom-ai", [item["id"] for item in api.storage_scan_status()["ai_detected_items"]])

    def test_ai_exclusion_rejects_drive_root(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / "ai_exclusions.json"

            class API(FakeStorageAPI):
                pass

            install_storage_guards(API, store_path=store)
            result = API().storage_ai_exclusions_add("C:\\")
            self.assertFalse(result["ok"])
            self.assertFalse(store.exists())


if __name__ == "__main__":
    unittest.main()
