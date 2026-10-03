def parse_resources(text, previous=None):
    resources = dict(previous or {})
    for entry in text.split(","):
        entry = entry.strip()
        if not entry:
            continue
        cls, _, value = entry.partition("=")
        try:
            capacity = int(value)
        except ValueError:
            raise ValueError("expects CLASS=N, got '%s'" % entry)
        if capacity < 1:
            raise ValueError("capacity shall be at least 1 ('%s')" % entry)
        resources[cls] = capacity
    return resources

def format_resources(resources):
    return ",".join("%s=%d" % (cls, cap)
                    for cls, cap in sorted((resources or {}).items()))

# Plain TTY prompt for a feed's login/password pair. The TUI wires its
