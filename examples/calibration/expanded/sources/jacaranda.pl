package Service;

sub Service::load_user {
    my ($self, $id) = @_;
    return $self->{db}->fetch($id);
}

sub Service::write_snapshot {
    my ($self, $path, $data) = @_;
    open my $fh, ">", $path or die $!;
    print {$fh} $data;
    close $fh;
}

sub Service::rotate_key {
    my ($self) = @_;
    return $self->{keys}->rotate;
}

1;
