function flight_agent_verify(jobDirectory)
request = jsondecode(fileread(fullfile(jobDirectory,'request.json')));
project = jsondecode(fileread(fullfile(jobDirectory,'project.json')));
config = project.config;
root = fullfile(jobDirectory,'project');
originalDirectory = pwd;
cleanup = onCleanup(@() finish(originalDirectory));
Simulink.fileGenControl('set','CacheFolder',fullfile(jobDirectory,'cache'), ...
    'CodeGenFolder',fullfile(jobDirectory,'codegen'),'createDir',true);
progress(jobDirectory,'initializing');
modelPath = fullfile(root,config.model);
initializerPath = fullfile(root,config.initializer);
[modelFolder,model] = fileparts(modelPath);
[initializerFolder,initializer] = fileparts(initializerPath);
addpath(root,modelFolder,initializerFolder);
for index = 1:numel(config.dependencies)
    entry = fullfile(root,config.dependencies{index});
    if isfolder(entry)
        addpath(entry);
    else
        addpath(fileparts(entry));
    end
end
nativeBuilds = {};
for index = 1:numel(config.native_modules)
    module = config.native_modules(index);
    folder = fullfile(root,module.directory);
    addpath(folder);
    binary = fullfile(folder,[module.name '.' mexext]);
    if ~isfile(binary)
        if isempty(module.source) || ~isfile(fullfile(root,module.source))
            error('flight_agent:NativeDependency', ...
                'Missing platform-native module %s. Required extension: %s.',module.name,mexext);
        end
        cd(folder);
        try
            mex('-outdir',folder,'-output',module.name,fullfile(root,module.source));
        catch buildError
            error('flight_agent:NativeDependency', ...
                'Cannot build %s for %s in the isolated copy. Configure a supported C compiler: %s', ...
                module.name,mexext,buildError.message);
        end
        nativeBuilds{end+1} = struct('module',module.name,'extension',mexext);
    end
end
cd(initializerFolder);
if ~isempty(config.trim_selector)
    [selectorFolder,selector] = fileparts(fullfile(root,config.trim_selector));
    addpath(selectorFolder);
    feval(selector,double(request.trim_point));
end
if strcmp(config.initializer_kind,'function')
    feval(initializer);
else
    evalin('base',['run(''' strrep(initializerPath,'''','''''') ''')']);
end
selectedTrim = verifyTrim(config,request.trim_point);
load_system(modelPath);
if ~isempty(config.working_directory)
    workingDirectory = fullfile(root,config.working_directory);
else
    workingDirectory = modelFolder;
end
cd(workingDirectory);
set_param(model,'ReturnWorkspaceOutputs','on','SimulationMode','normal');
output = struct('run_id',request.id,'project_fingerprint',request.project_fingerprint, ...
    'trim_point',request.trim_point,'matlab_version',version,'analyses',{{}}, ...
    'provenance',struct('model',config.model,'initializer',config.initializer, ...
    'selection',selectedTrim,'native_builds',{nativeBuilds},'platform',computer));
for index = 1:numel(request.analyses)
    kind = request.analyses{index};
    progress(jobDirectory,kind);
    try
        cd(workingDirectory);
        verifyTrim(config,request.trim_point);
        switch kind
            case 'step_response'
                record = timeAnalysis(model,config,jobDirectory);
            case 'stability_margins'
                record = frequencyAnalysis(model,config,jobDirectory,workingDirectory);
            otherwise
                error('flight_agent:UnsupportedTool','Unsupported analysis: %s',kind);
        end
        verifyTrim(config,request.trim_point);
    catch analysisError
        record = struct('kind',kind,'execution_status','failed','valid',false, ...
            'metrics',struct,'diagnostics',{{getReport(analysisError,'extended','hyperlinks','off')}}, ...
            'details',struct('failure_stage',kind),'artifact_paths',{{}});
    end
    output.analyses{end+1} = record;
    writeJson(fullfile(jobDirectory,'analysis.json'),output);
end
progress(jobDirectory,'analyzed');
end

function selection = verifyTrim(config,point)
selection = struct('point_index',point,'verification','registered initializer');
if ~isempty(config.trim_selection_variable)
    actual = evalin('base',config.trim_selection_variable);
    if ~isstruct(actual) || ~isfield(actual,config.trim_selection_field) ...
            || ~isequal(double(actual.(config.trim_selection_field)),double(point))
        error('flight_agent:TrimMismatch','Initialized trim point does not match the confirmed point.');
    end
    selection = actual;
end
end

function record = timeAnalysis(model,config,folder)
simulation = sim(model,'ReturnWorkspaceOutputs','on');
logs = simulation.get('logsout');
if ~isa(logs,'Simulink.SimulationData.Dataset')
    error('flight_agent:MissingLogs','The model must provide a logsout Dataset.');
end
input = logs.get(config.input_signal.name).Values;
response = logs.get(config.response_signal.name).Values;
if ~isa(input,'timeseries') || ~isa(response,'timeseries') ...
        || numel(input.Data) ~= numel(input.Time) || numel(response.Data) ~= numel(response.Time)
    error('flight_agent:SignalShape','Registered input and response must be scalar timeseries.');
end
if ~any(strcmpi(input.TimeInfo.Units,{'seconds','second','s'})) ...
        || ~any(strcmpi(response.TimeInfo.Units,{'seconds','second','s'}))
    error('flight_agent:TimeUnit','Signal time axes must be in seconds.');
end
record = flight_agent_step_metrics(input.Time,input.Data,response.Time,response.Data, ...
    config.steady_state);
record.details.input_signal = config.input_signal;
record.details.response_signal = config.response_signal;
record.details.stop_time = get_param(model,'StopTime');
record.details.solver = get_param(model,'Solver');
record.details.fixed_step = get_param(model,'FixedStep');
record.artifact_paths = {'step-response.mat','step-response.png'};
save(fullfile(folder,'step-response.mat'),'input','response','record','-v7');
figureHandle = figure('Visible','off','Position',[100 100 1000 700]);
cleanup = onCleanup(@() close(figureHandle));
subplot(2,1,1);
plot(input.Time,input.Data,'LineWidth',1.2);
grid on;
ylabel(['Stick (' config.input_signal.unit ')'],'Interpreter','none');
title('Actual nonlinear input and registered response');
subplot(2,1,2);
plot(response.Time,response.Data,'LineWidth',1.2);
grid on;
xlabel('Time (s)');
ylabel([config.response_signal.name ' (' config.response_signal.unit ')'],'Interpreter','none');
if isfield(record.details,'step_time')
    xline(record.details.step_time,'--','Step');
end
if record.valid
    final = record.details.final_value;
    band = 0.05*abs(final-record.details.initial_value);
    yline(final+band,':','5% band');
    yline(final-band,':');
    title(sprintf('Overshoot %.5g%%; settling %.5g s', ...
        record.metrics.overshoot.value,record.metrics.settling_time.value));
else
    title('Insufficient valid evidence for step metrics');
end
exportgraphics(figureHandle,fullfile(folder,'step-response.png'),'Resolution',120);
end

function record = frequencyAnalysis(model,config,folder,workingDirectory)
io = getlinio(model);
types = {io.Type};
if sum(strcmp(types,'openinput')) ~= 1 || sum(strcmp(types,'openoutput')) ~= 1 ...
        || numel(io) ~= 2
    error('flight_agent:LinearizationPoints', ...
        'D1 requires one registered openinput and one openoutput, without guessed points.');
end
if any(~strcmp({io.Active},'on'))
    error('flight_agent:InactivePoints','Registered linearization points must be active.');
end
cd(workingDirectory);
if strcmp(config.operating_point_mode,'workspace')
    operatingPoint = evalin('base',config.operating_point_variable);
else
    operatingPoint = operpoint(model);
end
loop = config.feedback_sign*linearize(model,operatingPoint,io);
record = flight_agent_margin_metrics(loop);
record.details.operating_point_mode = config.operating_point_mode;
record.details.feedback_sign = config.feedback_sign;
record.details.linearization_points = arrayfun(@(point) struct( ...
    'block',point.Block,'port',point.PortNumber,'type',point.Type),io,'UniformOutput',false);
record.artifact_paths = {'stability-margins.mat','bode.png'};
save(fullfile(folder,'stability-margins.mat'),'loop','operatingPoint','io','record','-v7');
figureHandle = figure('Visible','off','Position',[100 100 1000 700]);
cleanup = onCleanup(@() close(figureHandle));
bode(loop);
grid on;
title('Registered open-loop frequency response');
exportgraphics(figureHandle,fullfile(folder,'bode.png'),'Resolution',120);
end

function progress(folder,stage)
writeJson(fullfile(folder,'progress.json'),struct('stage',stage));
end

function writeJson(path,value)
temporary = [path '.tmp'];
file = fopen(temporary,'w','n','UTF-8');
if file < 0
    error('flight_agent:Output','Cannot write %s',temporary);
end
cleanup = onCleanup(@() fclose(file));
fprintf(file,'%s',jsonencode(value));
clear cleanup;
movefile(temporary,path,'f');
end

function finish(originalDirectory)
bdclose('all');
cd(originalDirectory);
end
