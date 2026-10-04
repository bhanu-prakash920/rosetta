Feature: The agent proposes, people decide
  The agent may read, test and propose. It may never change what runs in production.

  Background:
    Given a running platform with 2000 simulated vehicles
    And "helix" has been sending for 30 seconds
    And the agent has submitted a validated draft for "helix"

  Scenario Outline: The registry refuses state changes from the agent
    When the agent tries to "<action>" its own draft
    Then the registry refuses with "forbidden"
    And the draft is still in state "validated"
    And the refusal is recorded in the audit log

    Examples:
      | action   |
      | approve  |
      | promote  |
      | rollback |
      | retire   |

  Scenario: Every tool call of the agent is on record
    Then each step of the agent run has an audit entry
    And the audit chain is intact

  Scenario: A mapping with the wrong unit cannot be approved
    Given an engineer writes a mapping for "helix" that reads speed in the wrong unit
    When the mapping is tested against the golden set
    Then it stays in state "draft"
    And an engineer cannot approve it

  Scenario: History cannot be rewritten unnoticed
    When someone edits an old audit entry in the database
    Then verifying the audit chain reports where it breaks
