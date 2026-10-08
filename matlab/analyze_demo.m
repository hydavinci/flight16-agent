function output_json = analyze_demo(input_json)
% Software demonstration only; not a flight model or certification method.
p = jsondecode(input_json);
required = {'mass_kg', 'wing_area_m2', 'airspeed_mps', 'air_density_kg_m3'};
for i = 1:numel(required)
    name = required{i};
    validateattributes(p.(name), {'numeric'}, {'scalar', 'real', 'finite', 'positive'});
end
validateattributes(p.thrust_n, {'numeric'}, {'scalar', 'real', 'finite', 'nonnegative'});
assert(strcmp(p.schema_version, '1.0'), 'Unsupported input schema');
assert(strcmp(p.aircraft_type, 'demo_fixed_wing'), 'Unsupported aircraft type');
assert(strcmp(p.condition, 'demo_cruise'), 'Unsupported condition');

weight = p.mass_kg * 9.80665;
pressure = 0.5 * p.air_density_kg_m3 * p.airspeed_mps^2;
values = [weight / p.wing_area_m2, pressure, ...
    weight / (pressure * p.wing_area_m2), p.thrust_n / weight];
assert(all(isfinite(values)), 'Nonfinite demo result');

result.schema_version = '1.0';
result.aircraft_type = p.aircraft_type;
result.condition = p.condition;
result.model_version = 'demo-static-v1';
result.matlab_version = version;
result.valid = true;
result.diagnostics = {};
result.metrics.wing_loading = struct('value', values(1), 'unit', 'N/m^2');
result.metrics.dynamic_pressure = struct('value', values(2), 'unit', 'Pa');
result.metrics.required_lift_coefficient = struct('value', values(3), 'unit', '1');
result.metrics.thrust_to_weight = struct('value', values(4), 'unit', '1');
output_json = jsonencode(result);
end
