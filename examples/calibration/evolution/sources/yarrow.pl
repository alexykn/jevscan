package Trail;

sub Trail::advance {
    my ($resource, $item) = @_;
    $resource->replace($item);
    $resource->advance($item->{key});
    return $resource->state;
}

1;
