function flight_agent_execute_workbench(run_dir, filename, kind, parameters_json, request_sha256, max_figures)
set(groot, 'defaultFigureVisible', 'off');
existing = findall(groot, 'Type', 'figure');
params = jsondecode(parameters_json);
source_path = fullfile(run_dir, 'code', filename);
result = execute_source(source_path, kind, params);

created = setdiff(findall(groot, 'Type', 'figure'), existing);
assert(numel(created) <= max_figures, 'Figure count exceeds the limit');
output.request_sha256 = request_sha256;
output.result = result;
output.matlab_version = version;
output.figures = cell(1, numel(created));
for i = 1:numel(created)
    output.figures{i} = sprintf('figure-%d.png', i);
    exportgraphics(created(i), fullfile(run_dir, output.figures{i}), 'Resolution', 120);
end
encoded = jsonencode(output, 'ConvertInfAndNaN', false);
file = fopen(fullfile(run_dir, 'receipt.json'), 'w', 'n', 'UTF-8');
assert(file ~= -1, 'Cannot write execution receipt');
cleanup = onCleanup(@() fclose(file));
fprintf(file, '%s', encoded);
close(created);
end

function result = execute_source(source_path, kind, params)
[source_dir, entry] = fileparts(source_path);
cd(source_dir);
if strcmp(kind, 'function')
    result = feval(entry, params);
else
    run(source_path);
    assert(exist('result', 'var') == 1, 'Script must assign a result variable');
end
end
