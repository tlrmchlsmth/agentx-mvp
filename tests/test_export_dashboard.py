import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "export_dashboard", Path(__file__).resolve().parents[1] / "export_dashboard.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class GrafanaScopingTests(unittest.TestCase):
    def test_bare_metrics_are_scoped_to_serving_pods(self):
        expr = 'rate(vllm:prefix_cache_hits_total[1m]) / rate(vllm:prefix_cache_queries_total[1m])'
        scoped = exporter.scope_promql_expr(expr, "glm-prefill|glm-decode")
        self.assertEqual(scoped.count('pod=~"glm-prefill|glm-decode"'), 2)
        self.assertIn('vllm:prefix_cache_hits_total{pod=~"glm-prefill|glm-decode"}[1m]', scoped)

    def test_existing_selectors_and_quoted_metric_names(self):
        expr = 'sum(vllm:num_requests_running{job="serving"}) + label_replace(DCGM_FI_DEV_GPU_UTIL, "x", "vllm:fake", "y", ".*")'
        scoped = exporter.scope_promql_expr(expr, "glm")
        self.assertIn('vllm:num_requests_running{job="serving", pod=~"glm"}', scoped)
        self.assertIn('DCGM_FI_DEV_GPU_UTIL{pod=~"glm"}', scoped)
        self.assertIn('"vllm:fake"', scoped)
        self.assertEqual(scoped.count('pod=~"glm"'), 2)

    def test_inference_pool_metrics_use_campaign_namespace(self):
        expr = 'inference_pool_ready_pods{name=~".*"} + rate(inference_pool_requests_total[1m])'
        scoped = exporter.scope_promql_expr(expr, "glm-prefill|glm-decode", "campaign-ns")
        self.assertIn('inference_pool_ready_pods{name=~".*", namespace="campaign-ns"}', scoped)
        self.assertIn('inference_pool_requests_total{namespace="campaign-ns"}[1m]', scoped)


if __name__ == "__main__":
    unittest.main()
