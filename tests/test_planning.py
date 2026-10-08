"""Planning class: classification, config, and delivery through both hooks."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOKS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "plugins", "claude-model-router-hook", "hooks"
)
sys.path.insert(0, HOOKS_DIR)
from router import config, taxonomy  # noqa: E402
from router.config import DEFAULTS, load_config  # noqa: E402
from router.policy import main_prompt_decision  # noqa: E402


def _det_cfg():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["classifier"]["cli_fallback"] = False
    return cfg


def _run_hook(script, payload, home):
    env = dict(os.environ)
    env["HOME"] = home
    for key in ("CLAUDE_MODEL_ROUTER_CHILD", "CLAUDE_PLUGIN_ROOT",
                "CLAUDE_PLUGIN_DATA", "CLAUDE_CODE_SUBAGENT_MODEL",
                "ANTHROPIC_MODEL"):
        env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, os.path.join(HOOKS_DIR, script)],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, cwd=home,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _home(tmp, overrides=None):
    cfg = {"version": 2, "classifier": {"cli_fallback": False}}
    cfg.update(overrides or {})
    os.makedirs(os.path.join(tmp, ".claude"), exist_ok=True)
    with open(os.path.join(tmp, ".claude", "model-router.json"), "w") as fh:
        json.dump(cfg, fh)
    return tmp


class TestPlanningClassification(unittest.TestCase):

    PLANNING = (
        "plan the implementation of the new billing export",
        "make a plan for adding SSO",
        "write an implementation plan for the caching layer",
        "give me a step-by-step plan to migrate auth to OAuth",
        "let's plan the rollout of feature flags",
        "plan out the refactor of the payments module",
        "roadmap for moving the frontend to TypeScript",
        "break the onboarding epic down into tickets",
        "how should we approach adding offline support?",
        "use plan mode for this: add rate limiting to the API",
        "before writing any code, figure out how to add search",
    )

    NOT_PLANNING = (
        "create the subscription plan model with monthly and annual prices",
        "add a plan selector to the checkout page",
        "rename PlanCard to PricingCard",
        "fix the bug where free plan users see premium features",
        "update the pricing plan table styles",
        "write a test for the Plan component",
    )

    def test_explicit_plan_requests_classify_as_planning(self):
        cfg = _det_cfg()
        for prompt in self.PLANNING:
            klass, _ = taxonomy.classify(prompt, cfg, None)
            self.assertEqual(klass, "planning", prompt)

    def test_plan_as_a_noun_is_not_planning(self):
        cfg = _det_cfg()
        for prompt in self.NOT_PLANNING:
            klass, _ = taxonomy.classify(prompt, cfg, None)
            self.assertNotEqual(klass, "planning", prompt)

    def test_planning_intent_counts_as_task_intent(self):
        self.assertTrue(taxonomy.has_task_intent("roadmap for the v3 release"))

    def test_planning_wins_the_tie_with_implementation(self):
        """planning precedes implementation in CLASSES, so a tie lands on it."""
        self.assertLess(
            taxonomy.CLASSES.index("planning"),
            taxonomy.CLASSES.index("implementation"),
        )

    def test_planning_intent_skips_extreme_escalation(self):
        result = taxonomy.score(
            "plan the multi-region active-active migration across the whole "
            "codebase", _det_cfg(),
        )
        self.assertEqual(result.top, "planning")
        self.assertEqual(result.scores["extreme"], 0)

    def test_plan_mode_is_no_longer_an_architecture_keyword(self):
        self.assertNotIn("plan mode", taxonomy.DEFAULT_KEYWORDS["architecture"])


class TestPlanningConfig(unittest.TestCase):

    def test_default_planning_target_is_fable_high(self):
        self.assertEqual(
            DEFAULTS["classes"]["planning"]["target"],
            {"model": "fable", "effort": "high"},
        )

    def test_planning_delivery_defaults_to_delegate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(global_path=os.path.join(tmp, "absent.json"), cwd=tmp)
        self.assertEqual(cfg["planning_delivery"], "delegate")

    def test_invalid_planning_delivery_is_repaired(self):
        cfg = config._normalize_config({"planning_delivery": "teleport"})
        self.assertEqual(cfg["planning_delivery"], "delegate")

    def test_switch_is_accepted(self):
        cfg = config._normalize_config({"planning_delivery": "switch"})
        self.assertEqual(cfg["planning_delivery"], "switch")

    def test_same_tier_planning_keeps_high_effort(self):
        cfg = _det_cfg()
        cfg["classes"]["planning"]["target"] = {"model": "opus", "effort": "low"}
        decision = main_prompt_decision(
            "planning", "claude-opus-5-5", "low", cfg, None, "make a plan"
        )
        self.assertEqual((decision.model, decision.effort), ("opus", "high"))


class TestPlanningPromptDelivery(unittest.TestCase):

    EVENT = {
        "prompt": "write an implementation plan for adding SSO",
        "model": "claude-opus-5-5",
        "effort": {"level": "high"},
    }

    def _context(self, stdout):
        return json.loads(stdout)["hookSpecificOutput"]["additionalContext"]

    def test_delegate_mode_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, _ = _run_hook(
                "user_prompt_submit.py", self.EVENT,
                _home(tmp, {"allow_fable_autoswitch": True}),
            )
        self.assertEqual(code, 0)
        output = json.loads(stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        context = output["additionalContext"]
        self.assertIn('"Create an implementation plan for"', context)
        self.assertIn("wait for their approval", context)
        self.assertNotIn('model: "fable"', context)

    def test_delegate_pins_the_model_when_the_fable_gate_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, _ = _run_hook(
                "user_prompt_submit.py", self.EVENT,
                _home(tmp, {"allow_fable_autoswitch": False}),
            )
        self.assertEqual(code, 0)
        self.assertIn('Pass model: "fable" explicitly', self._context(stdout))

    def test_delegate_pins_the_model_when_enforcement_is_not_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, _ = _run_hook(
                "user_prompt_submit.py", self.EVENT,
                _home(tmp, {"allow_fable_autoswitch": True,
                            "subagent_enforcement": "advisory"}),
            )
        self.assertEqual(code, 0)
        self.assertIn('Pass model: "fable" explicitly', self._context(stdout))

    def test_switch_mode_keeps_the_blocking_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, stderr = _run_hook(
                "user_prompt_submit.py", self.EVENT,
                _home(tmp, {"planning_delivery": "switch"}),
            )
        self.assertEqual(code, 2)
        self.assertIn("/model fable", stderr)

    def test_fable_session_plans_in_place(self):
        event = dict(self.EVENT, model="claude-fable-5-1")
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, stderr = _run_hook(
                "user_prompt_submit.py", event, _home(tmp)
            )
        self.assertEqual((code, stdout, stderr), (0, "", ""))


class TestPlanningSubagentRouting(unittest.TestCase):

    def _spawn(self, tool_input, overrides=None):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, _ = _run_hook(
                "pre_tool_use.py",
                {"tool_name": "Agent", "tool_input": tool_input},
                _home(tmp, overrides),
            )
        self.assertEqual(code, 0)
        return json.loads(stdout)

    def test_plan_subagent_type_forces_planning(self):
        for subagent_type in ("Plan", "planner", "PLANNING"):
            output = self._spawn(
                {"subagent_type": subagent_type, "prompt": "look around the repo"},
                {"allow_fable_autoswitch": True},
            )
            updated = output["hookSpecificOutput"]["updatedInput"]
            self.assertEqual(updated["model"], "fable", subagent_type)
            self.assertEqual(updated["subagent_type"], subagent_type)

    def test_gated_fable_advisory_names_the_class(self):
        output = self._spawn(
            {"subagent_type": "Plan", "prompt": "look around the repo"},
            {"allow_fable_autoswitch": False},
        )
        self.assertIn("planning-class (fable-tier)", output["systemMessage"])
        self.assertNotIn("updatedInput", output["hookSpecificOutput"])


if __name__ == "__main__":
    unittest.main()
