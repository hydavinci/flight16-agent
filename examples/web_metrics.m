function result = web_metrics(params)
% Demo metrics compatible with this project's demo rules; not flight certification.
fields = {'mass_kg', 'wing_area_m2', 'airspeed_mps', 'air_density_kg_m3'};
for i = 1:numel(fields)
    validateattributes(params.(fields{i}), {'numeric'}, {'scalar', 'positive', 'finite'});
end
validateattributes(params.thrust_n, {'numeric'}, {'scalar', 'nonnegative', 'finite'});
assert(strcmp(params.aircraft_type, 'demo_fixed_wing'), 'Unsupported demo aircraft');
assert(strcmp(params.condition, 'demo_cruise'), 'Unsupported demo condition');
assert(strcmp(params.schema_version, '1.0'), 'Unsupported demo schema');
weight = params.mass_kg * 9.80665;
pressure = 0.5 * params.air_density_kg_m3 * params.airspeed_mps^2;
values = [weight / params.wing_area_m2, pressure, ...
    weight / (pressure * params.wing_area_m2), params.thrust_n / weight];
assert(all(isfinite(values)), 'Nonfinite demo result');
result = struct('schema_version', '1.0', 'aircraft_type', params.aircraft_type, ...
    'condition', params.condition, 'model_version', 'demo-static-v1', ...
    'matlab_version', version, 'valid', true, 'diagnostics', {{}});
result.metrics.wing_loading = struct('value', values(1), 'unit', 'N/m^2');
result.metrics.dynamic_pressure = struct('value', values(2), 'unit', 'Pa');
result.metrics.required_lift_coefficient = struct('value', values(3), 'unit', '1');
result.metrics.thrust_to_weight = struct('value', values(4), 'unit', '1');
disp('Demo metrics calculated; this is not an airworthiness assessment.');
end
