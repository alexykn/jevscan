sub format_for_wire {
    my ($value) = @_;
    my ($year, $month, $day) = split /-/, $value;
    return join("", $year, $month, $day);
}

sub format_for_log {
    my ($value) = @_;
    my ($year, $month, $day) = split /-/, $value;
    return join(" ", $year, $month, $day);
}
