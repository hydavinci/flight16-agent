function flight_agent_local_stepinfo(requestPath)
% Fixed independent metric bridge; the Python worker verifies all provenance.
request = jsondecode(fileread(requestPath));
output = request.output_directory;
series = jsondecode(fileread(request.series_path));
assert(strcmp(series.input.time_unit,'s') && strcmp(series.response.time_unit,'s'), ...
    'stepinfo requires normalized seconds');
support = fileparts(mfilename('fullpath'));
assert(strcmp(which('flight_agent_step_metrics'), ...
    fullfile(support,'flight_agent_step_metrics.m')), 'Packaged metric helper resolution mismatch');
record = flight_agent_step_metrics(series.input.time,series.input.values, ...
    series.response.time,series.response.values,request.inputs.steady_state, ...
    request.inputs.settling_band);
record.details.mode = 'stepinfo';
record.details.input_signal = struct('name',series.input.name,'unit',series.input.unit);
record.details.response_signal = struct('name',series.response.name,'unit',series.response.unit);
save(fullfile(output,'raw.mat'),'series','record','-v7');
% Preserve the verified serialized samples exactly, not a MATLAB JSON round trip.
copyfile(request.series_path,fullfile(output,'series.json'));
figureHandle = figure('Visible','off','Position',[100 100 1000 700]);
cleanup = onCleanup(@() close(figureHandle)); %#ok<NASGU>
subplot(2,1,1);
plot(series.input.time,series.input.values,'LineWidth',1.2);
grid on;
ylabel([series.input.name ' (' series.input.unit ')'],'Interpreter','none');
title('Published input and response');
subplot(2,1,2);
plot(series.response.time,series.response.values,'LineWidth',1.2);
grid on;
xlabel('Time (s)');
ylabel([series.response.name ' (' series.response.unit ')'],'Interpreter','none');
if isfield(record.details,'step_time')
    xline(record.details.step_time,'--','Step');
end
if isfield(record.details,'final_value')
    final = record.details.final_value;
    band = record.details.settling_band*abs(final-record.details.initial_value);
    yline(final+band,':',sprintf('%g%% band',100*record.details.settling_band));
    yline(final-band,':');
end
if record.valid
    title(sprintf('Overshoot %.5g%%; settling %.5g s', ...
        record.metrics.overshoot.value,record.metrics.settling_time.value));
else
    title('Invalid step evidence; see retained diagnostics');
end
exportgraphics(figureHandle,fullfile(output,'step-response.png'),'Resolution',120);
runtime.id = request.id;
runtime.request_sha256 = request.request_sha256;
runtime.snapshot_root = request.snapshot_root;
runtime.working_directory = pwd;
runtime.matlab_version = version;
runtime.matlab_release = version('-release');
runtime.products = ver;
runtime.settings = struct;
write_json(fullfile(output,'runtime-metadata.json'),runtime);
bridge.id = request.id;
bridge.request_sha256 = request.request_sha256;
bridge.artifact_paths = {'raw.mat','runtime-metadata.json','series.json','step-response.png'};
bridge.output_evidence = true;
bridge.valid = record.valid;
bridge.metrics = record.metrics;
bridge.details = record.details;
bridge.diagnostics = record.diagnostics;
write_json(fullfile(output,'bridge.json'),bridge);
end

function write_json(path,value)
file = fopen(path,'w','n','UTF-8');
assert(file >= 0,'Cannot open metric evidence file');
cleanup = onCleanup(@() fclose(file)); %#ok<NASGU>
fprintf(file,'%s',jsonencode(value));
end
