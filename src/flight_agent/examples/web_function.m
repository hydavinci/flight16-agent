function result = web_function(params)
t = linspace(0, 2, params.samples);
y = params.amplitude * cos(2 * pi * params.frequency_hz * t);
plot(t, y);
xlabel('Time (s)');
ylabel('Amplitude');
grid on;
result = struct('samples', params.samples, 'peak', max(y), 'minimum', min(y));
end
