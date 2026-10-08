function result = web_function(params)
% Single-file function example; no external MATLAB dependencies.
validateattributes(params.frequency_hz, {'numeric'}, {'scalar', 'positive', 'finite'});
validateattributes(params.amplitude, {'numeric'}, {'scalar', 'nonnegative', 'finite'});
validateattributes(params.samples, {'numeric'}, {'scalar', 'integer', '>=', 2, '<=', 10000});
t = linspace(0, 2, params.samples);
y = params.amplitude * sin(2 * pi * params.frequency_hz * t);
result = struct('time_s', t, 'signal', y, 'peak', max(abs(y)), ...
    'sample_count', numel(y));
figure;
plot(t, y, 'LineWidth', 1.5);
xlabel('Time (s)');
ylabel('Signal');
title('Uploaded MATLAB function');
grid on;
disp('Waveform calculation completed.');
end
