function flight_agent_run_project(fa_request_path)
% Versioned bridge: existing trusted code only, never generated expressions.
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
fa_return = [];
fa_initializer_return = [];
if ~isempty(fa_inputs.initializer)
    fa_path = fullfile(fa_root, fa_inputs.initializer.path);
    if strcmp(fa_inputs.initializer.kind, 'script')
        run(fa_path);
    else
        fa_initializer_return = call_existing(fa_path, fa_request.initializer_arguments, ...
            fa_request.initializer_nargout);
    end
end
fa_before_names = who;
fa_metadata.matlab_version = version;
fa_metadata.matlab_release = version('-release');
fa_metadata.products = ver;
fa_metadata.conditions = fa_inputs.conditions;
fa_metadata.inputs = fa_inputs;
fa_metadata.id = fa_request.id;
fa_metadata.request_sha256 = fa_request.request_sha256;
fa_metadata.snapshot_root = fa_root;
fa_metadata.working_directory = pwd;
fa_metadata.settings = struct;
fa_log_name = 'logsout';
if strcmp(fa_inputs.mode, 'entry')
    fa_path = fullfile(fa_root, fa_inputs.entry.path);
    if strcmp(fa_inputs.entry.kind, 'script')
        run(fa_path);
    else
        fa_return = call_existing(fa_path, fa_request.entry_arguments, fa_request.entry_nargout);
    end
else
    fa_model_path = fullfile(fa_root, fa_inputs.model);
    [~, fa_model] = fileparts(fa_model_path);
    load_system(fa_model_path);
    assert(same_file(get_param(fa_model, 'FileName'), fa_model_path), ...
        'Selected model resolution mismatch');
    fa_parameters = {'StartTime', 'StopTime', 'Solver', 'SolverType', ...
        'FixedStep', 'SignalLogging', 'SignalLoggingName', 'SaveOutput', ...
        'OutputSaveName', 'SaveTime', 'TimeSaveName', 'ReturnWorkspaceOutputs'};
    for fa_index = 1:numel(fa_parameters)
        fa_name = fa_parameters{fa_index};
        fa_metadata.settings.(fa_name) = get_param(fa_model, fa_name);
    end
    fa_log_name = get_param(fa_model, 'SignalLoggingName');
    % Keep model configuration, inputs and logging intact; initializer locals
    % are available to this simulation, not shared with any later node.
    fa_return = sim(fa_model, 'SrcWorkspace', 'current');
    if isa(fa_return, 'Simulink.SimulationOutput')
        fa_metadata.simulation_model_info = fa_return.SimulationMetadata.ModelInfo;
        fa_metadata.simulation_timing_info = fa_return.SimulationMetadata.TimingInfo;
    end
end
fa_names = who;
fa_names = fa_names(~startsWith(fa_names, 'fa_'));
fa_new_names = setdiff(fa_names, fa_before_names);
fa_info = whos;
assert(sum([fa_info.bytes]) <= 100 * 1024 * 1024, 'Workspace exceeds raw artifact limit');
fa_raw_path = fullfile(fa_output, 'raw.mat');
save(fa_raw_path, fa_names{:}, 'fa_return', 'fa_initializer_return', '-v7');
fa_receipt.id = fa_request.id;
fa_receipt.request_sha256 = fa_request.request_sha256;
fa_receipt.artifact_paths = {'raw.mat', 'metadata.json'};
fa_receipt.output_evidence = has_result(fa_return) || ~isempty(fa_new_names);
fa_receipt.details.mode = fa_inputs.mode;
fa_receipt.diagnostics = {};
if ~isempty(fa_inputs.input_signal)
    fa_workspace = load(fa_raw_path);
    fa_series.input = selected_series(fa_inputs.input_signal, fa_workspace, fa_return, fa_log_name);
    fa_series.response = selected_series( ...
        fa_inputs.response_signal, fa_workspace, fa_return, fa_log_name);
    write_json(fullfile(fa_output, 'series.json'), fa_series);
    fa_receipt.artifact_paths{end + 1} = 'series.json';
    fa_receipt.output_evidence = true;
else
    % Opaque/non-JSON outputs remain original MAT evidence, never synthesized.
    if has_result(fa_return) && json_result(fa_return, 0)
        write_json(fullfile(fa_output, 'return.json'), struct('result', fa_return));
        fa_receipt.artifact_paths{end + 1} = 'return.json';
    elseif has_result(fa_return)
        fa_receipt.diagnostics = {'Returned shape is retained as raw MAT only.'};
    end
end
write_json(fullfile(fa_output, 'metadata.json'), fa_metadata);
write_json(fullfile(fa_output, 'bridge.json'), fa_receipt);
end

function result = call_existing(path, arguments_json, output_count)
[~, name] = fileparts(path);
assert(same_file(which(name), path), 'Selected function resolution mismatch');
assert(nargin(name) == numel(arguments_json) && nargout(name) == output_count, ...
    'Unsupported runtime function signature');
call_arguments = cell(1, numel(arguments_json));
for index = 1:numel(arguments_json)
    call_arguments{index} = jsondecode(arguments_json{index});
end
if output_count == 1
    result = feval(name, call_arguments{:});
else
    feval(name, call_arguments{:});
    result = [];
end
end

function same = same_file(actual, expected)
% fileattrib resolves relative filenames without expression evaluation.
[ok_actual, actual_info] = fileattrib(actual);
[ok_expected, expected_info] = fileattrib(expected);
same = ok_actual && ok_expected && strcmp(actual_info.Name, expected_info.Name);
end

function series = selected_series(selection, workspace, returned, log_name)
switch selection.source
    case 'return'
        value = returned;
    case 'workspace'
        value = workspace;
    case 'logsout'
        if isa(returned, 'Simulink.SimulationOutput')
            value = get(returned, log_name);
        else
            assert(isfield(workspace, log_name), 'Missing logsout workspace evidence');
            value = workspace.(log_name);
        end
    otherwise
        error('Unsupported signal source');
end
for index = 1:numel(selection.path)
    name = selection.path{index};
    if isa(value, 'Simulink.SimulationData.Signal')
        value = value.Values;
    end
    if isa(value, 'Simulink.SimulationOutput')
        value = get(value, name);
    elseif isa(value, 'Simulink.SimulationData.Dataset')
        names = getElementNames(value);
        assert(sum(strcmp(names, name)) == 1, 'Signal name is missing or ambiguous');
        value = getElement(value, name);
    elseif isstruct(value) && isscalar(value) && isfield(value, name)
        value = value.(name);
    else
        error('Unsupported or missing explicit signal field: %s', name);
    end
end
if isa(value, 'Simulink.SimulationData.Signal')
    value = value.Values;
end
if isa(value, 'timeseries')
    assert(isempty(value.TimeInfo.StartDate), 'Absolute timeseries dates are unsupported');
    time = value.Time;
    values = value.Data;
    time_unit = value.TimeInfo.Units;
    actual_unit = value.DataInfo.Units;
elseif isstruct(value) && isscalar(value) && all(isfield(value, {'time', 'values', 'time_unit'}))
    time = value.time;
    values = value.values;
    time_unit = value.time_unit;
    actual_unit = '';
    if isfield(value, 'unit')
        actual_unit = value.unit;
    end
else
    error('Unsupported series shape: require timeseries or explicit time/values/time_unit');
end
assert(isnumeric(time) && isreal(time) && isvector(time) && ...
    isnumeric(values) && isreal(values) && isvector(values), 'Only scalar numeric series supported');
assert(numel(time) >= 2 && numel(time) <= 100000 && numel(time) == numel(values), ...
    'Series length exceeds limit or does not match');
assert(all(isfinite(time(:))) && all(isfinite(values(:))) && all(diff(time(:)) > 0), ...
    'Series must contain finite, strictly ordered samples');
assert(ischar(actual_unit) || isstring(actual_unit), 'Unsupported signal unit metadata');
assert(isempty(actual_unit) || strcmp(actual_unit, selection.unit), 'Signal unit mismatch');
series.time = double(time(:));
series.values = double(values(:));
% The Python worker normalizes these actual units before publishing seconds.
series.time_unit = time_unit;
series.unit = selection.unit;
series.name = strjoin(selection.path, '.');
end

function evidence = has_result(value)
evidence = ~isempty(value);
if isa(value, 'Simulink.SimulationOutput')
    names = who(value);
    evidence = false;
    for index = 1:numel(names)
        if ~isempty(get(value, names{index}))
            evidence = true;
            return;
        end
    end
elseif isstruct(value)
    evidence = evidence && ~isempty(fieldnames(value));
end
end

function supported = json_result(value, depth)
supported = false;
if depth > 32 || numel(value) > 100000
    return;
end
if isnumeric(value)
    supported = isreal(value) && all(isfinite(value(:)));
elseif islogical(value) || ischar(value)
    supported = true;
elseif isstruct(value) && isscalar(value)
    names = fieldnames(value);
    supported = true;
    for index = 1:numel(names)
        if ~json_result(value.(names{index}), depth + 1)
            supported = false;
            return;
        end
    end
elseif iscell(value)
    supported = true;
    for index = 1:numel(value)
        if ~json_result(value{index}, depth + 1)
            supported = false;
            return;
        end
    end
end
end

function write_json(path, value)
encoded = jsonencode(value, 'ConvertInfAndNaN', false);
assert(numel(unicode2native(encoded, 'UTF-8')) <= 4000000, 'JSON output exceeds limit');
temporary = [path '.tmp'];
file = fopen(temporary, 'w', 'n', 'UTF-8');
assert(file ~= -1, 'Cannot open execution output');
cleanup = onCleanup(@() fclose(file));
fprintf(file, '%s', encoded);
clear cleanup;
[ok, message] = movefile(temporary, path, 'f');
assert(ok, message);
end
