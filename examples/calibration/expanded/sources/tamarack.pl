sub format_date {
    my ($value) = @_;
    my ($year, $month, $day) = split /-/, $value;
    return join("/", $month, $day, $year);
}

sub display_date {
    my ($value) = @_;
    my ($year, $month, $day) = split /-/, $value;
    return join("/", $month, $day, $year);
}

sub label_date {
    my ($value) = @_;
    return format_date($value);
}
