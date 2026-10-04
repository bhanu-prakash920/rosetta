Feature: A firmware update changes a maker's format
  Software-defined vehicles change their data format over the air. After an update,
  part of a fleet speaks the new format while the rest has not updated yet.

  Background:
    Given a running platform with 2000 simulated vehicles
    And the known makers are sending telemetry

  Scenario: Drifted messages are parked with a precise reason
    When 40 percent of "pacifica" vehicles receive the firmware update
    And the fleet runs for 4 seconds
    Then "pacifica" messages are parked with the reason "SCHEMA_MISMATCH"
    And the most failing field is "pacifica.speed_kmh"
    And "pacifica" vehicles on the old firmware are still translated

  Scenario: A canary reads the new format while the live version keeps serving the old one
    Given 40 percent of "pacifica" vehicles have received the firmware update
    And the agent has submitted a validated draft for "pacifica"
    When an engineer approves the draft for 25 percent of vehicles
    And the fleet runs for 4 seconds
    Then "pacifica" has live version 1 and canary version 2
    And both versions translate events
    And no new "pacifica" message is parked for "SCHEMA_MISMATCH"

  Scenario: The unit change that no field name reveals is found by physics
    Given 40 percent of "pacifica" vehicles have received the firmware update
    When an engineer asks the agent for a mapping for "pacifica"
    Then the draft maps the source field "speed" to "speed_kmh" as "speed|kmh"
    And the draft maps the source field "odometer" to "odo_km" as "odo|km"
