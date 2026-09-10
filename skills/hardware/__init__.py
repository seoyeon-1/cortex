"""Phase 7.1 - Hardware abstraction layer (skills/hardware).

DigitalTwinClient wraps a *profile* (ros2 / carla / klipper / opentrons / secs-gem). With no
endpoint configured (the safe default) every client runs against a local deterministic
simulation; the API shape is identical either way:

    simulate(action) -> predicted_state
    deploy(firmware_bin) -> device_status          # dry_run journals the exact flash plan
    telemetry_stream() -> AsyncIterator[SensorData]

Nothing here ever talks to real hardware unless the operator explicitly flips the
`hardware.dry_run: false` flag AND provides an endpoint - dual-use by design.
"""
