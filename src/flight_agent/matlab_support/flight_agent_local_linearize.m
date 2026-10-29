function flight_agent_local_linearize(fa_request_path)
% Fixed local bridge: existing entries, saved points, no generated expressions.
fa_request = jsondecode(fileread(fa_request_path));
fa_inputs = fa_request.inputs;
fa_root = fa_request.snapshot_root;
fa_output = fa_request.output_directory;
if ~isempty(fa_inputs.project_file)
    fa_project = openProject(fullfile(fa_root, fa_inputs.project_file)); %#ok<NASGU>
end
for fa_index = 1:numel(fa_request.project_paths)
    addpath(fa_request.project_paths{fa_index}, '-end');
end
if isempty(fa_inputs.working_directory)
    cd(fa_root);
else
    cd(fullfile(fa_root, fa_inputs.working_directory));
end
Simulink.fileGenControl('set', 'CacheFolder', fullfile(fa_root, '.flight-cache'), ...
    'CodeGenFolder', fullfile(fa_root, '.flight-codegen'), 'createDir', true);
fa_preparations = {};
for fa_index = 1:numel(fa_inputs.preparations)
    fa_entry = fa_inputs.preparations(fa_index).entry;
    fa_path = fullfile(fa_root, fa_entry.path);
    if strcmp(fa_entry.kind, 'script')
        assert(~isfile([fa_path(1:end-2) '.p']), 'Selected script has same-stem P-file');
        sync_workspace('base', 'caller');
        run(fa_path);
        sync_workspace('caller', 'base');
    else
        call_existing(fa_path, fa_request.preparations(fa_index));
        sync_workspace('base', 'caller');
    end
    fa_preparations{end+1} = fa_entry.path;
end
fa_initializer = '';
if ~isempty(fa_inputs.initializer)
    fa_path = fullfile(fa_root, fa_inputs.initializer.path);
    if strcmp(fa_inputs.initializer.kind, 'script')
        assert(~isfile([fa_path(1:end-2) '.p']), 'Selected script has same-stem P-file');
        sync_workspace('base', 'caller');
        run(fa_path);
    else
        call_existing(fa_path, fa_request.initializer);
        sync_workspace('base', 'caller');
    end
    fa_initializer = fa_inputs.initializer.path;
end
% Scripts run in this bridge's local workspace; functions may assign base.
% Publish script locals into this node's fresh Engine, without discarding base.
fa_names = who;
fa_names = fa_names(~startsWith(fa_names, 'fa_'));
fa_info = whos;
assert(sum([fa_info.bytes]) <= 100*1024*1024, 'Initializer workspace exceeds limit');
save(fullfile(fa_output, 'raw.mat'), fa_names{:}, '-v7');
fa_workspace = load(fullfile(fa_output, 'raw.mat'));
for fa_index = 1:numel(fa_names)
    assignin('base', fa_names{fa_index}, fa_workspace.(fa_names{fa_index}));
end
fa_assertions = verify_assertions(fa_inputs);
fa_model_path = fullfile(fa_root, fa_inputs.model);
[~, fa_model] = fileparts(fa_model_path);
load_system(fa_model_path);
assert(same_file(get_param(fa_model, 'FileName'), fa_model_path), ...
    'Selected model resolution mismatch');
fa_io = getlinio(fa_model);
assert(numel(fa_io) == 2 && sum(strcmp({fa_io.Type}, 'openinput')) == 1 ...
    && sum(strcmp({fa_io.Type}, 'openoutput')) == 1 ...
    && all(strcmp({fa_io.Active}, 'on')), ...
    'Only an existing active SISO preset openinput/openoutput pair is supported');
if strcmp(fa_inputs.operating_point.mode, 'workspace')
    fa_operating_point = workspace_field(fa_inputs.operating_point.path);
else
    fa_operating_point = operpoint(fa_model);
end
fa_loop = fa_inputs.feedback_sign * linearize(fa_model, fa_operating_point, fa_io);
assert(size(fa_loop,1) == 1 && size(fa_loop,2) == 1 && ndims(fa_loop) == 2, ...
    'Only a single SISO linear model is supported');
assertions_after = verify_assertions(fa_inputs);
assert(isequal(fa_assertions, assertions_after), 'Selected workspace conditions changed');
clear assertions_after;
fa_provenance.id = fa_request.id;
fa_provenance.request_sha256 = fa_request.request_sha256;
fa_provenance.model = fa_inputs.model;
fa_provenance.initializer = fa_initializer;
fa_provenance.preparations = fa_preparations;
fa_provenance.operating_point = struct('mode', fa_inputs.operating_point.mode, ...
    'path', {fa_inputs.operating_point.path});
fa_provenance.feedback_sign = fa_inputs.feedback_sign;
fa_provenance.workspace_assertions = fa_assertions;
fa_provenance.linearization_points = arrayfun(@(point) struct( ...
    'block', point.Block, 'port', point.PortNumber, 'type', point.Type, ...
    'active', point.Active), fa_io, 'UniformOutput', false);
fa_provenance.siso = true;
fa_provenance.sample_time = fa_loop.Ts;
loop = fa_loop;
provenance = fa_provenance;
save(fullfile(fa_output, 'linear-model.mat'), 'loop', 'provenance', '-v7');
save(fullfile(fa_output, 'raw.mat'), 'fa_operating_point', 'fa_io', ...
    'loop', 'provenance', '-append');
fa_runtime.id = fa_request.id;
fa_runtime.request_sha256 = fa_request.request_sha256;
fa_runtime.snapshot_root = fa_root;
fa_runtime.working_directory = pwd;
fa_runtime.matlab_version = version;
fa_runtime.matlab_release = version('-release');
fa_runtime.products = ver;
fa_runtime.settings = struct('Solver', get_param(fa_model, 'Solver'), ...
    'StartTime', get_param(fa_model, 'StartTime'), 'StopTime', get_param(fa_model, 'StopTime'));
write_json(fullfile(fa_output, 'runtime-metadata.json'), fa_runtime);
fa_bridge.id = fa_request.id;
fa_bridge.request_sha256 = fa_request.request_sha256;
fa_bridge.output_evidence = true;
fa_bridge.artifact_paths = {'linear-model.mat', 'raw.mat', 'runtime-metadata.json'};
fa_bridge.details = struct('mode', 'linearize', 'linearization', fa_provenance);
fa_bridge.diagnostics = {};
write_json(fullfile(fa_output, 'bridge.json'), fa_bridge);
end

function sync_workspace(source, target)
% Share project state across entry kinds without overwriting bridge locals.
names = evalin(source, 'who');
names = names(~startsWith(names, 'fa_'));
for index = 1:numel(names)
    assert(isvarname(names{index}), 'Invalid workspace identifier');
    assignin(target, names{index}, evalin(source, names{index}));
end
end

function call_existing(path, call)
[~, name] = fileparts(path);
assert(same_file(which(name), path), 'Selected function resolution mismatch');
assert(nargin(name) == call.nargin && nargout(name) == call.nargout, ...
    'Runtime function signature differs from discovered signature');
call_arguments = cell(1, numel(call.arguments));
for index = 1:numel(call_arguments)
    call_arguments{index} = jsondecode(call.arguments{index});
end
if call.nargin < 0
    assert(numel(call_arguments) >= -call.nargin-1, 'Missing fixed function arguments');
else
    assert(numel(call_arguments) <= call.nargin, 'Too many function arguments');
end
if call.nargout > 0
    returned = feval(name, call_arguments{:}); %#ok<NASGU>
else
    feval(name, call_arguments{:});
end
end

function assertions = verify_assertions(inputs)
assertions = {};
for index = 1:numel(inputs.workspace_assertions)
    assertion = inputs.workspace_assertions(index);
    actual = workspace_field(assertion.path);
    expected = inputs.conditions.(assertion.condition);
    assert(isequal(actual, expected), ...
        'Selected workspace condition mismatch: %s', strjoin(assertion.path, '.'));
    assertions{end+1} = struct('path', {assertion.path}, ...
        'condition', assertion.condition, 'actual', actual);
end
end

function value = workspace_field(path)
% Only validated identifiers and struct field traversal, never expressions.
assert(~isempty(path), 'Workspace path is required');
for index = 1:numel(path)
    assert(isvarname(path{index}), 'Invalid workspace identifier');
end
value = evalin('base', path{1});
for index = 2:numel(path)
    assert(isstruct(value) && isscalar(value) && isfield(value, path{index}), ...
        'Missing explicit workspace field');
    value = value.(path{index});
end
end

function same = same_file(actual, expected)
[ok_actual, actual_info] = fileattrib(actual);
[ok_expected, expected_info] = fileattrib(expected);
same = ok_actual && ok_expected && strcmp(actual_info.Name, expected_info.Name);
end

function write_json(path, value)
file = fopen(path, 'w', 'n', 'UTF-8');
assert(file >= 0, 'Cannot open linearization evidence');
cleanup = onCleanup(@() fclose(file)); %#ok<NASGU>
fprintf(file, '%s', jsonencode(value));
end
