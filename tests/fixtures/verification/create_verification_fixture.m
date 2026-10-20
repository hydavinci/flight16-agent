function create_verification_fixture(folder)
mkdir(folder);
model = 'verification_fixture';
if bdIsLoaded(model)
    close_system(model,0);
end
new_system(model);
cleanup = onCleanup(@() close_system(model,0));
add_block('simulink/Sources/Step',[model '/Stick'], ...
    'Time','1.5','Before','0','After','1');
add_block('simulink/Math Operations/Sum',[model '/Error'],'Inputs','+-');
add_block('simulink/Continuous/Transfer Fcn',[model '/Loop'], ...
    'Numerator','4','Denominator','[1 2 1]');
add_block('simulink/Sinks/Terminator',[model '/End']);
inputLine = add_line(model,'Stick/1','Error/1');
add_line(model,'Error/1','Loop/1');
responseLine = add_line(model,'Loop/1','End/1');
add_line(model,'Loop/1','Error/2');
set_param(get_param(inputLine,'SrcPortHandle'),'DataLogging','on','DataLoggingNameMode','Custom', ...
    'DataLoggingName','stick');
set_param(get_param(responseLine,'SrcPortHandle'),'DataLogging','on','DataLoggingNameMode','Custom', ...
    'DataLoggingName','response');
set_param(model,'StartTime','0','StopTime','15','SolverType','Fixed-step', ...
    'Solver','ode5','FixedStep','0.01','SignalLogging','on', ...
    'SignalLoggingName','logsout','ReturnWorkspaceOutputs','on');
io(1) = linio([model '/Error'],1,'openinput');
io(2) = linio([model '/Loop'],1,'openoutput');
setlinio(model,io);
save_system(model,fullfile(folder,[model '.slx']));
file = fopen(fullfile(folder,'initialize.m'),'w');
fprintf(file,'function initialize\nend\n');
fclose(file);
config = struct('schema_version','1.0','name','Synthetic verification fixture', ...
    'backend','simulink','model',[model '.slx'],'initializer','initialize.m', ...
    'initializer_kind','function', ...
    'input_signal',struct('name','stick','unit','1'), ...
    'response_signal',struct('name','response','unit','1'), ...
    'feedback_sign',1,'operating_point_mode','initialized');
file = fopen(fullfile(folder,'flight-agent.project.json'),'w');
fprintf(file,'%s',jsonencode(config));
fclose(file);
end
