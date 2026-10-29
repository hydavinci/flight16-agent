function flight_agent_local_allmargin(request_path)
% Only a verified dependency MAT, never project code or a supplied expression.
request = jsondecode(fileread(request_path));
output = request.output_directory;
data = load(request.linear_model_path, 'loop', 'provenance');
assert(all(isfield(data, {'loop', 'provenance'})), 'Missing linear model provenance');
assert(strcmp(data.provenance.id, request.producer_id) && ...
    strcmp(data.provenance.request_sha256, request.producer_request_sha256), ...
    'Linear model producer identity mismatch');
loop = data.loop;
assert(isa(loop, 'ss') || isa(loop, 'tf') || isa(loop, 'zpk'), ...
    'Unsupported linear model type');
support = fileparts(mfilename('fullpath'));
assert(strcmp(which('flight_agent_margin_metrics'), ...
    fullfile(support, 'flight_agent_margin_metrics.m')), 'Packaged margin helper resolution mismatch');
record = flight_agent_margin_metrics(loop);
% The shared mathematical helper remains unchanged for existing Worker jobs.
% Normalize every special explicitly; MATLAB jsonencode alone loses infinities.
record.details.mode = 'allmargin';
if record.valid
    assert(isscalar(record.details.closed_loop_stable) && ...
        ismember(record.details.closed_loop_stable, [0 1]), 'Unknown nominal stability');
    record.details.closed_loop_stable = logical(record.details.closed_loop_stable);
    record.metrics.gain_margin = normalize_metric(record.metrics.gain_margin);
    record.metrics.phase_margin = normalize_metric(record.metrics.phase_margin);
    for role = {'gain_crossings', 'phase_crossings'}
        name = role{1};
        for index = 1:numel(record.details.(name))
            crossing = record.details.(name){index};
            crossing.margin = normalize_metric(crossing.margin);
            crossing.frequency = frequency_metric(crossing.frequency);
            record.details.(name){index} = crossing;
        end
    end
    % Normalized crossings stay in record; the original loop stays in MAT.
    record.details = rmfield(record.details, 'allmargin');
end
save(fullfile(output, 'raw.mat'), 'data', 'record', '-v7');
copyfile(request.linear_model_path, fullfile(output, 'linear-model.mat'));
copyfile(fullfile(request.snapshot_root, 'producer-metadata.json'), ...
    fullfile(output, 'producer-metadata.json'));
figure_handle = figure('Visible', 'off', 'Position', [100 100 1000 700]);
cleanup = onCleanup(@() close(figure_handle)); %#ok<NASGU>
bode(loop);
grid on;
title('Published open-loop frequency response');
exportgraphics(figure_handle, fullfile(output, 'bode.png'), 'Resolution', 120);
runtime.id = request.id;
runtime.request_sha256 = request.request_sha256;
runtime.snapshot_root = request.snapshot_root;
runtime.working_directory = pwd;
runtime.matlab_version = version;
runtime.matlab_release = version('-release');
runtime.products = ver;
runtime.settings = struct;
write_json(fullfile(output, 'runtime-metadata.json'), runtime);
bridge.id = request.id;
bridge.request_sha256 = request.request_sha256;
bridge.artifact_paths = {'raw.mat', 'runtime-metadata.json', 'linear-model.mat', ...
    'producer-metadata.json', 'bode.png'};
bridge.output_evidence = true;
bridge.valid = record.valid;
bridge.metrics = record.metrics;
bridge.details = record.details;
bridge.diagnostics = record.diagnostics;
write_json(fullfile(output, 'bridge.json'), bridge);
end

function metric = normalize_metric(metric)
metric.valid = true;
if isfield(metric, 'special')
    metric.value = NaN;
else
    assert(isreal(metric.value) && isscalar(metric.value) && isfinite(metric.value), ...
        'Undefined or unsupported numeric margin');
end
end

function metric = frequency_metric(value)
assert(isnumeric(value) && isreal(value) && isscalar(value) && ...
    (isnan(value) || value >= 0), 'Unsupported crossover frequency');
metric = struct('value', value, 'unit', 'rad/s', 'valid', true);
if isnan(value)
    metric.value = NaN;
    metric.valid = false;
    metric.special = 'undefined';
    metric.reason = 'No corresponding crossover frequency.';
elseif isinf(value)
    metric.value = NaN;
    metric.special = 'positive_infinity';
end
end

function write_json(path, value)
file = fopen(path, 'w', 'n', 'UTF-8');
assert(file >= 0, 'Cannot open allmargin evidence');
cleanup = onCleanup(@() fclose(file)); %#ok<NASGU>
fprintf(file, '%s', jsonencode(value));
end
