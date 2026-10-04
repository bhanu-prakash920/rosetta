Feature: A new car maker joins without downtime
  As a platform engineer
  I want to onboard a telemetry source the platform has never seen
  So that its vehicles appear for customers without a release, a restart or lost data

  Background:
    Given a running platform with 2000 simulated vehicles
    And the known makers are sending telemetry

  Scenario: Messages from an unknown maker are parked, not dropped
    When "helix" starts sending telemetry
    And the fleet runs for 4 seconds
    Then no "helix" event has been translated
    And every "helix" message is parked with the reason "NO_ADAPTER"
    And the known makers are still translated without interruption

  Scenario: The agent proposes a mapping that passes the golden set
    Given "helix" has been sending for 30 seconds
    When an engineer asks the agent for a mapping for "helix"
    Then the agent submits a draft in state "validated"
    And the draft passes 100 percent of the golden cases
    And the draft maps the source field "fahrt.v" to "speed_kmh" as "speed|mps"
    And nothing reads the draft yet

  Scenario: Approval makes the parked messages flow, with no restart
    Given "helix" has been sending for 30 seconds
    And the agent has submitted a validated draft for "helix"
    When an engineer approves and promotes the draft
    Then the workers load the mapping without restarting
    And every parked "helix" message that was not damaged is replayed
    And new "helix" telemetry is translated directly
    And no event was counted twice
