Feature: The stream is unreliable and the platform is not
  Networks duplicate, delay and reorder messages. None of that may change the result.

  Background:
    Given a running platform with 2000 simulated vehicles

  Scenario: Duplicate events are dropped exactly once
    Given the network duplicates 20 percent of messages
    When the fleet runs for 6 seconds
    Then every duplicate that was sent is dropped
    And every event that was sent is accounted for
    And the archive holds each event once

  Scenario: Out-of-order events do not move a vehicle backwards
    Given the network delays 30 percent of messages
    When the fleet runs for 8 seconds
    Then the latest state of each vehicle is its newest event

  Scenario: A killed worker resumes without loss
    Given the network is clean
    When the fleet runs for 3 seconds without being processed
    And the normaliser is killed after its first batch
    And a replacement normaliser starts
    Then every event that was sent is accounted for
    And no event was counted twice

  Scenario: An alert reaches the alert stream within five seconds
    Given the network is clean
    When a vehicle brakes hard
    Then a "HARSH_BRAKE" alert is raised for that vehicle
    And it was raised less than 5000 milliseconds after the event arrived
