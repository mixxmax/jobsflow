from xml.etree.ElementTree import fromstring

from tools.check_coverage import _module_branch


def test_branch_coverage_weights_conditions_not_lines():
    root = fromstring(
        """
        <coverage>
          <packages><package name="tools"><classes>
            <class filename="workflow/example.py">
              <lines>
                <line number="1" branch="true" condition-coverage="0% (0/1)" />
                <line number="2" branch="true" condition-coverage="100% (4/4)" />
              </lines>
            </class>
          </classes></package></packages>
        </coverage>
        """
    )
    assert _module_branch(root, "tools/workflow/example.py") == 80.0


def test_branch_coverage_ignores_malformed_condition_data():
    root = fromstring(
        """
        <coverage><class filename="workflow/example.py"><lines>
          <line number="1" branch="true" condition-coverage="unknown" />
        </lines></class></coverage>
        """
    )
    assert _module_branch(root, "tools/workflow/example.py") is None
