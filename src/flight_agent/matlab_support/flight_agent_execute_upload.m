function flight_agent_execute_upload(job_dir, filename, kind, parameters_json, max_figures)
% Only explicitly trusted uploaded programs may enter this wrapper.
set(groot, 'defaultFigureVisible', 'off');
params = jsondecode(parameters_json);
source_path = fullfile(job_dir, 'code', filename);
result = execute_source(source_path, kind, params);

output.result = result;
output.matlab_version = version;
figures = findall(groot, 'Type', 'figure');
assert(numel(figures) <= max_figures, 'Figure count exceeds the limit');
output.figures = cell(1, numel(figures));
for i = 1:numel(figures)
    output.figures{i} = sprintf('figure-%d.png', i);
    exportgraphics(figures(i), fullfile(job_dir, output.figures{i}), 'Resolution', 120);
end
encoded = jsonencode(output, 'ConvertInfAndNaN', false);
file = fopen(fullfile(job_dir, 'output.json'), 'w', 'n', 'UTF-8');
assert(file ~= -1, 'Cannot write execution result');
cleanup = onCleanup(@() fclose(file));
fprintf(file, '%s', encoded);
close all force;
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
