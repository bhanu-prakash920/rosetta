Feature: Personal data is protected
  Location and driver identity are personal data under GDPR and India's DPDP Act.

  Background:
    Given a running platform with 2000 simulated vehicles
    And the known makers are sending telemetry
    And the fleet has run for 10 seconds

  Scenario: A fleet manager sees only their own tenant
    Given I am signed in as "manager@northwind.example"
    When I list vehicles
    Then every vehicle belongs to tenant 1
    And a vehicle of another tenant answers "404"

  Scenario: An analyst sees masked locations and shortened vehicle numbers
    Given I am signed in as "analyst@rosetta.example"
    When I list vehicles
    Then every vehicle number shows only its last 6 characters
    And every position is the centre of a geohash cell of 5 characters

  Scenario: A driver is erased on request
    Given I am signed in as "manager@northwind.example"
    And a driver of my tenant
    When I request erasure of that driver
    Then the request is "completed" and verified
    And the driver has no name, e-mail, phone or licence on record
    And no vehicle is linked to the driver any more
    And the erasure itself is in the audit log

  Scenario: Reading the audit log needs a staff role
    Given I am signed in as "manager@northwind.example"
    When I open the audit log
    Then I am refused with "403"
